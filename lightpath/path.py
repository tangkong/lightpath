"""
The :class:`.BeamPath` is the main abstraction for the lightpath module,
grouping together a set of devices using the :class:`.LightInterface` and
representing the path between them as single object. While the manipulation of
each of these object should be done at the device level, the
:meth:`.BeamPath.clear` does provide a powerful tool to quickly change the
status of the path.

The :class:`.BeamPath` object is also not meant to be a rigid representation,
:meth:`.BeamPath.split` and :meth:`.BeamPath.join` both allow for slicing and
combining of different areas of the LCLS beamline. However, keep in mind that
the path only knows the state of the devices it contains, so certain methods
might not return an accurate representation of reality if an upstream device is
affecting the beam.
"""
from __future__ import annotations

import enum
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, List, Optional, TextIO, Tuple, Union

from ophyd import Device, DeviceStatus
from ophyd.ophydobj import OphydObject
from ophyd.status import wait as status_wait
from ophyd.utils import DisconnectedError
from prettytable import PrettyTable

from .errors import CoordinateError

logger = logging.getLogger(__name__)


@dataclass
class LightpathState:
    inserted: bool
    removed: bool
    transmission: float
    output_branch: str


class DeviceState(enum.IntEnum):
    """
    Description of BeamStates

    The standard Inserted, Removed or Unknown have been expanded within
    this state to help operators diagnose exact reasons for uncertainty in the
    state of the beamline

    Attributes
    ----------
    Removed:
        Device is removed from the beamline.

    Inserted:
        Device is inserted into the beamline. This may or may not prevent beam
        from reaching downstream devices.

    Unknown:
        Device is reporting neither an inserted or removed state.

    Inconsistent:
        The device is reporting that is both inserted and removed.

    Disconnected:
        We were unable to determine the state of the device because one or more
        of the relevant signals was not available.

    Error:
        Catch-all state for any errors the device reported when asked for its
        state that were not simply a failure to communicate with signals
    """
    Removed = 0
    Inserted = 1
    Unknown = 2
    Inconsistent = 3
    Disconnected = 4
    Error = 5


def find_device_state(device: Device) -> Tuple[DeviceState, LightpathState]:
    """
    Report the state of a device

    The device must implement ``.inserted`` and ``removed``.

    Parameters
    ----------
    device : Device
        ophyd Device implementing the Lightpath interface

    Returns
    -------
    Tuple[DeviceState, LightpathState]
        DeviceState enum
        LightpathState dataclass
    """
    # Gather device information
    try:
        state = device.get_lightpath_state()
        _in, _out = state.inserted, state.removed
        logger.debug("Device %s reporting; IN=%s, OUT=%s",
                     device.name, _in, _out)
    # Check if this was an error with an EPICS connection
    except (TimeoutError, DisconnectedError) as exc:
        logger.warning("Unable to connect to %r", device)
        logger.debug(exc, exc_info=True)
        return DeviceState.Disconnected, None
    except Exception:
        logger.exception("Unable to determine device state for %r", device)
        return DeviceState.Error, None
    # Check state consistency and return proper Enum
    # In
    if _in and not _out:
        return DeviceState.Inserted, state
    # Out
    elif _out and not _in:
        return DeviceState.Removed, state
    # Both In and Out
    elif _out and _in:
        return DeviceState.Inconsistent, state
    # Neither In or Out
    else:
        return DeviceState.Unknown, state


class BeamPath(OphydObject):
    """
    Represents a straight line of devices along the beamline

    The devices given must be a continuous set all along the same beamline, or,
    multiple beamlines with appropriate reflecting devices in between.

    Parameters
    ----------
    devices : :class:`.LightDevice`
        Arguments are interpreted as LightDevices along a common beamline.

    name = str, optional
        Name of the BeamPath

    Raises
    ------
    TypeError:
        If a non-LightDevice object is supplied

    CoordinateError:
        If a coordinate is not properly specified

    PathError:
        If multiple beamlines are present, with no reflecting device

    Attributes
    ----------
    minimum_transmission : float
        Minimum amount of transmission considered for beam presence
    """
    # Subscription Information
    SUB_PTH_CHNG = 'beampath_changed'
    _default_sub = SUB_PTH_CHNG
    # Transmission setting
    minimum_transmission = 0.1

    def __init__(self, *devices: OphydObject, name: Optional[str] = None):
        super().__init__(name=name)
        self.devices = devices
        self._has_subscribed = False
        logger.debug("Configuring path %s with %s devices",
                     name, len(self.devices))
        # Sort by position downstream to upstream
        try:
            # Check types and positions
            for dev in self.path:
                # Ensure positioning is physical
                if math.isnan(dev.md.z) or dev.md.z < 0.:
                    raise CoordinateError('Device %r is reporting a '
                                          'non-existant beamline position, '
                                          'its coordinate was not properly '
                                          'initialized', dev)
                # Add as attribute
                setattr(self, dev.name.replace(' ', '_'), dev)

        except AttributeError as e:
            raise TypeError('One of the devices does not meet the '
                            'neccesary lightpath interface. Missing '
                            'attribute {}'.format(e))

    @property
    def branching_devices(self) -> List[Device]:
        """ List[Device]: Branching devices along the path """
        return [d for d in self.devices
                if len(getattr(d, 'output_branches', ['1'])) > 1]

    @property
    def range(self) -> Tuple[float, float]:
        """ Tuple[float, float]: Starting z position of beamline """
        return self.path[0].md.z, self.path[-1].md.z

    @property
    def path(self) -> List[Device]:
        """ List[Device]: List of devices ordered by coordinates """
        return sorted(self.devices, key=lambda dev: dev.md.z)

    @property
    def blocking_devices(self) -> List[Device]:
        """
        A list of devices that are currently inserted or are in unknown
        positions. This includes devices downstream of the first
        :attr:`.impediment`

        Returns
        -------
        List[Device]
            list of blockoing devices
        """
        # Cache important prior devices
        prev_device = None
        prev_status = None
        block = list()
        for device in self.path:
            curr_state, curr_status = find_device_state(device)

            # short circuit if statuses are in error
            if curr_state in (DeviceState.Error, DeviceState.Unknown):
                block.append(device)

            # check to make sure input and output branches match
            # e.g. mirror not pointing to current device
            elif (prev_device is not None and prev_status is not None and
                  prev_status.output_branch not in device.input_branches):
                block.append(prev_device)
            # check inserted
            elif curr_state is DeviceState.Inserted:
                # Ignore devices with low enough transmssion
                if curr_status.transmission < self.minimum_transmission:
                    block.append(device)
            # Find unknown and inconsistent devices
            elif curr_state is not DeviceState.Removed:
                block.append(device)

            # stash previous device
            prev_device = device
            prev_status = curr_status

        return block

    @property
    def incident_devices(self) -> List[Device]:
        """
        A list of devices the beam is currently incident on. This includes the
        current :attr:`.impediment` and any upstream devices that may be
        inserted but have more transmission than :attr:`.minimum_transmission`

        Returns
        -------
        List[Device]
            List of incident devices
        """
        # Find device information
        inserted = [d for d in self.path
                    if find_device_state(d)[0] == DeviceState.Inserted]
        impediment = self.impediment
        # No blocking devices, all inserted devices incident
        if not impediment:
            return inserted
        # Otherwise only return upstream of the impediment
        return [d for d in inserted if d.md.z <= impediment.md.z]

    def show_devices(self, file: TextIO = None):
        """
        Print a table of the devices along the beamline

        Parameters
        ----------
        file : TextIO
            File-like object to write output to.  Default behavior is
            printing to sys.stdout
        """
        # Initialize Table
        pt = PrettyTable(['Name', 'Prefix', 'Position', 'Input Branches',
                          'Output Branches', 'State'])
        # Adjust Table settings
        pt.align = 'r'
        pt.align['Name'] = 'l'
        pt.align['Prefix'] = 'l'
        pt.float_format = '8.5'
        # Add info
        for d in self.path:
            pt.add_row([d.name, d.prefix, d.md.z, d.input_branches,
                        d.output_branches, find_device_state(d)[0].name])
        # Show table
        print(pt, file=file)

    @property
    def impediment(self) -> Device:
        """ Device: First blocking device along the path """
        # Find device information
        blocks = self.blocking_devices
        if not blocks:
            return None

        else:
            return blocks[0]

    @property
    def cleared(self) -> bool:
        """
        Whether beamline is clear of any devices that are below the
        :attr:`.minimum_transmission`

        Returns
        -------
        bool
            whether beamline is clear of impediments
        """
        return not any(self.blocking_devices)

    def clear(
        self,
        wait: bool = False,
        timeout: Optional[float] = None,
        ignore: Optional[List[Device]] = None,
        passive: bool = False,
    ) -> List[DeviceStatus]:
        """
        Clear the beampath of all obstructions

        Parameters
        ----------
        wait : bool, optional
            Wait for all devices to complete their motion

        timeout : float, optional
            Duration to wait for device movements

        ignore: device or iterable, optional
            Leave devices in their current state without removing them

        passive : bool, optional
            If False, devices that are inserted but don't attenuate the beam
            below :attr:`.minimum_threshold` are ignored

        Returns
        -------
        statuses : List[ophyd.DeviceStatus]
            Returns list of status objects returned by
            :meth:`.LightInterface.remove`
        """
        logger.info('Clearing beampath %s ...', self)
        # Assemble device list
        target_devices, ignored = self._ignore(ignore, passive=passive)
        # Remove devices
        logger.info('Removing devices along the beampath ...')
        status = [device.remove(timeout=timeout)
                  for device in target_devices
                  if find_device_state(device)[0] in (DeviceState.Inserted,
                                                      DeviceState.Unknown)
                  and hasattr(device, 'remove')]
        # Wait parameters
        if wait:
            logger.info('Waiting for all devices to be '
                        'removed from the beampath %s ...', self)
            # Wait consecutively for statuses, this can be done by combining
            # statuses in the future
            for s in status:
                logger.debug('Waiting for %s to be done ...', s)
                status_wait(s, timeout=timeout)
                logger.info('Completed')

        return status

    def join(self, *beampaths: BeamPath) -> BeamPath:
        """
        Join multiple beampaths with the current one

        Parameters
        ----------
        beampaths : arguments
            A list of beampaths to join into a complete path, order is
            irrelavant

        Returns
        -------
        BeamPath : :class:`.BeamPath`
            A new object with all of the path devices

        Raises
        ------
        TypeError:
            Raised if a non-BeamPath object is supplied
        """
        return BeamPath.from_join(self, *beampaths, name=self.name)

    def split(
        self,
        z: Optional[float] = None,
        device: Optional[Device] = None
    ) -> Tuple[BeamPath, BeamPath]:
        """
        Split the beampath producing two new BeamPath objects either by a
        specific position or a devices location

        Parameters
        ----------
        z : float
            Z position to split the paths

        device : LightDevice, name, or base PV
            The specified device will be the last device in the first
            :class:`.BeamPath` object

        Returns
        -------
        BeamPath, BeamPath
            Two new beampath instances
        """
        # Not enough information
        if not z and not device:
            raise ValueError(
                "Must supply where to split the path (either z or device)"
            )
        # Grab the z if given a device
        if device:
            z = device.md.z
        # Look within range
        if z < self.range[0] or z > self.range[1]:
            raise ValueError("Split position {} is not within the range of "
                             "the path.".format(z))
        # Split the paths
        return (BeamPath(*[d for d in self.devices if d.md.z <= z]),
                BeamPath(*[d for d in self.devices if d.md.z > z]))

    @classmethod
    def from_join(cls, *beampaths: BeamPath, name: str = None) -> BeamPath:
        """
        Join other beampaths with the current one

        Parameters
        ----------
        beampaths : arguments
            A list of beampaths to join into a complete path, order is
            irrelavant

        name : str, optional
            New name for created beampath

        Returns
        -------
        BeamPath : :class:`.BeamPath`
            A new object with all of the path devices

        Raises
        ------
        TypeError:
            Raised if a non-BeamPath object is supplied
        """
        # Catch invalid paths
        if not all(isinstance(bp, BeamPath) for bp in beampaths):
            raise TypeError('Can not join non-BeamPath object')
        # Flatten path lists
        devices = [device for path in beampaths for device in path.devices]
        # Create a new instance
        return BeamPath(*set(devices), name=name)

    def _ignore(
        self,
        ignore_devices: Optional[Union[Device, List[Device]]] = None,
        passive: bool = False
    ) -> Tuple[List[Device], List[Device]]:
        """
        Assemble list of available devices with some exclusions

        Parameters
        ----------
        ignore_devices : list
            Device(s) to ignore

        passive : bool
            If False, ignore passive devices

        Returns
        -------
        (target, ignore) : tuple
            Tuple of two lists of devices
        """
        ignore = list()

        # Add passive devices to ignored
        if not passive:
            logger.debug("Passive devices will be ignored ...")
            ignore.extend([d for d in self.devices
                           if d.get_lightpath_state().transmission >
                           self.minimum_transmission])
        # Add ignored devices
        if isinstance(ignore_devices, Iterable):
            ignore.extend(ignore_devices)
        elif ignore_devices:
            ignore.append(ignore_devices)
        # Grab target devices
        target_devices = [device for device in self.devices
                          if device not in ignore]
        logger.debug("Targeting devices %s ...", target_devices)
        logger.debug('Ignoring devices %s ...', ignore)
        return target_devices, ignore

    def _device_moved(self, *args, obj=None, **kwargs):
        """
        Run when a device changes state
        """
        # Determine whether our path has been changed
        block = self.impediment
        if block:
            block = block.md.z
        else:
            block = math.inf
        # If device is upstream of impediment
        if obj is not None and obj.parent.md.z <= block:
            self._run_subs(sub_type=self.SUB_PTH_CHNG, device=obj)

    def subscribe(
        self,
        cb: Callable,
        event_type: Optional[str] = None,
        run: bool = True
    ):
        """
        Subscribe to changes of the valve

        Parameters
        ----------
        cb : callable
            Callback to be run

        event_type : str, optional
            Type of event to run callback on

        run : bool, optional
            Run the callback immediatelly
        """
        if not self._has_subscribed:
            # Subscribe to all child devices
            for dev in self.devices:
                # Add callback here!
                try:
                    dev.lightpath_summary.subscribe(self._device_moved,
                                                    run=False)
                    # get once to initialize SummarySignal
                    dev.lightpath_summary.get()
                except Exception:
                    logger.error("BeamPath is unable to subscribe "
                                 "to device %s", dev.name)
            self._has_subscribed = True
        super().subscribe(cb, event_type=event_type, run=run)

    def _repr_info(self):
        yield('range',   self.range)
        yield('devices', len(self.devices))

    __hash = object.__hash__

    def __eq__(self, *args, **kwargs):
        try:
            return self.devices == args[0].devices
        except AttributeError:
            return super().__eq__(*args, **kwargs)
