"""
BINDER MK 53 (E2.1) climate chamber: Modbus RTU driver over RS-422.

Hardware      MB1 controller. RS-422 port: the DB25 socket on the lateral
              (left side) control panel. Only five pins are used:

                  DB25 pin   chamber signal   USB adapter terminal
                  2          RxD+             TXD+
                  4          RxD-             TXD-
                  3          TxD+             RXD+
                  5          TxD-             RXD-
                  7          GND              GND

Serial        9600 baud, 8 data bits, no parity, 1 stop bit
Protocol      Modbus RTU, function codes 0x03 (read) and 0x10 (write),
              CRC-16 (polynomial 0xA001, initial value 0xFFFF)
Floats        IEEE-754, word-swapped: the low 16-bit word is sent first
              (20.32 degC is sent as 8F 5C 41 A2)

Sources: BINDER operating manual MK (E2.1) 10/2014, chapter 14.1; BINDER
Interface Technical Specifications, Art. No. 7001-0242, issue 10/2024 (table
"Base address table for MB1 (with program option)"); BINDER wiring diagram
5612.3007 for article 9020-0006, connector -X10. Every value below matches
those documents, and the driver has been used on a real MK 53 (E2.1).

Register map (MB1 with program option):
  0x11A9  ADDR_CURTEMP     actual temperature (read, float32)
  0x1077  ADDR_SETPOINT    active setpoint (read, float32)
  0x1581  ADDR_MANSETPT    manual mode setpoint (write, float32)
  0x156F  ADDR_BASICSETPT  basic mode setpoint (write, float32)
  0x1A22  ADDR_MODE        operating mode (read, uint16)
              bit 12 (0x1000) basic, bit 11 (0x0800) manual,
              bit 10 (0x0400) auto (programme running), no bit set: idle

Errors:
  MK53CommError    no reply, garbled frame, wrong slave or CRC error (retried)
  MK53ModbusError  the chamber answered with a Modbus exception (not retried)
  ValueError       invalid argument (NaN, out of range, bad rate, ...)

Usage:
    from mk53_driver import MK53, resolve_port

    with MK53(resolve_port('auto'), slave_address=1) as chamber:
        print(chamber.get_temperature(), 'degC')
        chamber.set_temperature(22.0)

Framing credit: SiLab Bonn "basil" project (binder_mk53.py) and
ecree-solarflare/ovenctl.
"""

import math
import struct
import time

import pyvisa


FTDI_VID_PID = (0x0403, 0x6001)     # FT232R, the chip in the DSD TECH SH-U11 adapter


class MK53Error(Exception):
    """Base class for all MK53 driver errors."""


class MK53CommError(MK53Error):
    """No reply, short/garbled frame, wrong slave, or CRC mismatch (retryable)."""


class MK53ModbusError(MK53Error):
    """The chamber answered with a Modbus exception frame (not retryable)."""


def find_adapter_port():
    """VISA resource name of the FTDI FT232R USB adapter (e.g. 'ASRL12::INSTR').

    Returns None if no such adapter is plugged in. Raises MK53Error if there is
    more than one, because the right one cannot be guessed.
    """
    from serial.tools import list_ports
    found = [p.device for p in list_ports.comports()
             if (p.vid, p.pid) == FTDI_VID_PID]
    if not found:
        return None
    if len(found) > 1:
        raise MK53Error(f'More than one FTDI adapter found ({", ".join(found)}); '
                        f'choose one with --port')
    device = found[0]
    number = device[3:] if device.upper().startswith('COM') else device
    return f'ASRL{number}::INSTR'


def resolve_port(port=None):
    """Turn --port input into a VISA resource name.

    None or 'auto' looks for the FTDI adapter. 'COM5' becomes 'ASRL5::INSTR'.
    Anything else (already a resource name) is returned unchanged.
    """
    if port is None or port.lower() == 'auto':
        found = find_adapter_port()
        if found is None:
            raise MK53Error('No FTDI USB-RS422 adapter found. Plug it in, or name '
                            'the port with --port (for example --port COM5).')
        return found
    if port.upper().startswith('COM') and port[3:].isdigit():
        return f'ASRL{port[3:]}::INSTR'
    return port


def confirm_setpoint(chamber, value, tries=6, delay_s=0.4, tolerance_c=0.05):
    """Read the setpoint back until it matches `value`; returns the last readback.

    The controller takes a moment to apply a new setpoint, so reading it back
    straight after a write can still return the old value.
    """
    readback = None
    for _ in range(tries):
        time.sleep(delay_s)
        readback = chamber.get_temperature_setpoint()
        if abs(readback - value) <= tolerance_c:
            break
    return readback


class MK53:
    """
    Driver for the Binder MK 53 dynamic climate chamber.

    Uses a Modbus-RTU-compatible binary protocol over RS-422 serial.
    All register I/O is handled via raw byte framing with CRC-16.
    """

    # Modbus function codes
    _FC_READ  = 0x03    # Read N holding registers
    _FC_WRITE = 0x10    # Write N holding registers (with length byte + data)

    # Register addresses
    ADDR_CURTEMP    = 0x11A9    # Actual (measured) temperature — float32, 2 words
    ADDR_SETPOINT   = 0x1077    # Current setpoint readback — float32, 2 words
    ADDR_MANSETPT   = 0x1581    # Manual mode setpoint write — float32, 2 words
    ADDR_BASICSETPT = 0x156F    # Basic mode setpoint write  — float32, 2 words
    ADDR_MODE       = 0x1A22    # Operation mode flags — uint16, 1 word

    # Absolute hardware limits of the MK 53 (E2.1 manual: -40 to +180 °C)
    HW_MIN_TEMP = -40.0
    HW_MAX_TEMP = 180.0

    # Modbus error codes returned by the chamber
    _ERROR_CODES = {
        1: 'Invalid function',
        2: 'Invalid parameter address',
        3: 'Parameter value outside range',
        4: 'Slave not ready',
        5: 'Write access denied',
    }

    def __init__(
        self,
        resource_name: str,
        slave_address: int = 1,
        min_temp: float = -40.0,
        max_temp: float = 180.0,
        timeout_ms: int = 3000,
        retries: int = 3,
    ):
        """
        Parameters
        ----------
        resource_name  : PyVISA VISA address, e.g. 'ASRL3::INSTR'
        slave_address  : Modbus slave address set on the chamber (default 1)
        min_temp       : Safety lower bound for temperature setpoints (°C)
        max_temp       : Safety upper bound for temperature setpoints (°C)
        timeout_ms     : VISA read timeout in milliseconds
        retries        : Attempts per register transaction on comm errors

        Raises ValueError if the bounds are not finite, min >= max, or lie
        outside the chamber's hardware range (-40 to +180 °C).
        """
        if not (math.isfinite(min_temp) and math.isfinite(max_temp)):
            raise ValueError('min_temp and max_temp must be finite numbers')
        if not self.HW_MIN_TEMP <= min_temp < max_temp <= self.HW_MAX_TEMP:
            raise ValueError(
                f'Safety bounds must satisfy {self.HW_MIN_TEMP} <= min_temp < '
                f'max_temp <= {self.HW_MAX_TEMP} (got {min_temp}, {max_temp})'
            )
        if retries < 1:
            raise ValueError('retries must be at least 1')

        self.slave_address = slave_address
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.retries = retries

        rm = pyvisa.ResourceManager('@py')
        self._instr = rm.open_resource(resource_name)
        try:
            self._instr.timeout = timeout_ms

            # RS-422 serial settings — fixed by Binder MK53 hardware
            self._instr.baud_rate    = 9600
            self._instr.data_bits    = 8
            self._instr.stop_bits    = pyvisa.constants.StopBits.one
            self._instr.parity       = pyvisa.constants.Parity.none
            self._instr.flow_control = pyvisa.constants.ControlFlow.none
        except Exception:
            # do not leave the COM port locked if configuration fails
            self._instr.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._instr.close()

    # ------------------------------------------------------------------
    # Public API — temperature
    # ------------------------------------------------------------------

    def identify(self) -> str:
        """
        Return a brief identification string built from live register reads.
        (The MK53 has no *IDN? command — we compose one from known data.)
        """
        try:
            actual  = self.get_temperature()
            setpt   = self.get_temperature_setpoint()
            mode    = self.get_mode()
            return (f'Binder MK53  slave={self.slave_address}  '
                    f'actual={actual:.2f}°C  setpoint={setpt:.2f}°C  '
                    f'mode={mode}')
        except Exception as exc:
            return f'Binder MK53 (could not read registers: {exc})'

    def get_temperature(self, retries: int = None) -> float:
        """
        Read actual (measured) chamber temperature in °C.
        Comm errors (CRC, timeout, short frame) are retried; `retries`
        overrides the constructor default for this call.
        """
        words = self._read_registers(self.ADDR_CURTEMP, n_words=2,
                                     retries=retries)
        return self._decode_float(words)

    def get_temperature_setpoint(self) -> float:
        """Read the currently active temperature setpoint (°C)."""
        words = self._read_registers(self.ADDR_SETPOINT, n_words=2)
        return self._decode_float(words)

    def set_temperature(self, temperature_c: float):
        """
        Set temperature setpoint in °C.
        Writes to both ADDR_MANSETPT (manual mode) and ADDR_BASICSETPT
        (basic mode) so the setpoint is applied regardless of current mode.

        Raises ValueError if temperature is NaN/inf or outside
        [min_temp, max_temp].
        """
        if not math.isfinite(temperature_c):
            raise ValueError(f'Requested temperature {temperature_c!r} is not '
                             f'a finite number')
        if temperature_c < self.min_temp:
            raise ValueError(
                f'Requested temperature {temperature_c}°C is below '
                f'the safety minimum of {self.min_temp}°C'
            )
        if temperature_c > self.max_temp:
            raise ValueError(
                f'Requested temperature {temperature_c}°C exceeds '
                f'the safety maximum of {self.max_temp}°C'
            )
        words = self._encode_float(temperature_c)
        self._write_registers(self.ADDR_MANSETPT,   words)
        self._write_registers(self.ADDR_BASICSETPT, words)

    # ------------------------------------------------------------------
    # Public API — mode
    # ------------------------------------------------------------------

    def get_mode(self) -> list:
        """
        Return list of active mode strings: 'basic', 'manual', 'auto',
        or ['idle'] when no mode is active.
        """
        words = self._read_registers(self.ADDR_MODE, n_words=1)
        mode = words[0]
        modes = []
        if mode & 0x1000:
            modes.append('basic')
        if mode & 0x0800:
            modes.append('manual')
        if mode & 0x0400:
            modes.append('auto')
        return modes if modes else ['idle']

    # ------------------------------------------------------------------
    # Convenience: ramp and stability wait
    # ------------------------------------------------------------------

    def wait_for_stability(
        self,
        setpoint_c: float,
        tolerance_c: float = 0.5,
        stable_seconds: int = 60,
        timeout_seconds: int = 900,
        poll_interval_s: float = 5.0,
    ) -> bool:
        """
        Poll until actual temperature stays within ±tolerance_c of setpoint_c
        for stable_seconds continuously.

        Returns True when stable, False if timeout expires first.

        Example
        -------
        chamber.set_temperature(85.0)
        ok = chamber.wait_for_stability(85.0, tolerance_c=0.5,
                                        stable_seconds=60, timeout_seconds=600)
        """
        if not math.isfinite(setpoint_c):
            raise ValueError(f'setpoint_c {setpoint_c!r} is not a finite number')
        if poll_interval_s <= 0:
            raise ValueError('poll_interval_s must be greater than 0')

        stable_since = None
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            actual = self.get_temperature()
            if abs(actual - setpoint_c) <= tolerance_c:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif (time.monotonic() - stable_since) >= stable_seconds:
                    return True
            else:
                stable_since = None
            time.sleep(min(poll_interval_s,
                           max(0.0, deadline - time.monotonic())))

        return False

    def ramp_to(
        self,
        target_c: float,
        rate_c_per_min: float = 5.0,
        step_interval_s: float = 30.0,
    ):
        """
        Step the setpoint toward target_c at rate_c_per_min.
        Raises ValueError (before touching the chamber) if target_c is not
        finite or outside [min_temp, max_temp], or if rate/interval are <= 0.
        Blocks until target is set (does not wait for temperature to reach it).
        Use wait_for_stability() after this call if you need to wait.

        Parameters
        ----------
        target_c        : Final temperature setpoint (°C)
        rate_c_per_min  : Maximum rate of change in °C per minute
        step_interval_s : How often (seconds) to update the setpoint
        """
        # validate everything up front so a bad call never leaves a
        # half-finished ramp on the chamber
        if not math.isfinite(target_c):
            raise ValueError(f'target_c {target_c!r} is not a finite number')
        if not self.min_temp <= target_c <= self.max_temp:
            raise ValueError(
                f'Target {target_c}°C is outside the safety range '
                f'[{self.min_temp}, {self.max_temp}]°C'
            )
        if not (math.isfinite(rate_c_per_min) and rate_c_per_min > 0):
            raise ValueError('rate_c_per_min must be a positive number')
        if not (math.isfinite(step_interval_s) and step_interval_s > 0):
            raise ValueError('step_interval_s must be a positive number')

        current_sp = self.get_temperature_setpoint()
        if not math.isfinite(current_sp):
            raise MK53Error(f'Chamber returned an invalid setpoint '
                            f'({current_sp!r}); refusing to ramp')
        step_c = rate_c_per_min * (step_interval_s / 60.0)

        while abs(current_sp - target_c) > 0.05:
            if current_sp < target_c:
                current_sp = min(current_sp + step_c, target_c)
            else:
                current_sp = max(current_sp - step_c, target_c)
            self.set_temperature(current_sp)
            if current_sp != target_c:
                time.sleep(step_interval_s)

    # ------------------------------------------------------------------
    # Low-level Modbus framing
    # ------------------------------------------------------------------

    def _flush_input(self):
        """Discard stale bytes left over from an earlier failed transaction."""
        try:
            self._instr.flush(pyvisa.constants.BufferOperation.discard_read_buffer)
        except (NotImplementedError, pyvisa.errors.VisaIOError):
            pass    # backend cannot flush; the frame checks still catch garbage

    def _transact(self, req: bytes, expected_payload: int = None) -> bytes:
        """
        Send one request frame and return the complete, CRC-checked reply.

        The reply is read in stages because its length depends on its type:
        a Modbus exception frame is 5 bytes, a read reply is 5 + byte-count
        bytes and a write echo is 8 bytes.

        Raises MK53CommError (retryable) for I/O failure, a wrong slave or
        function code, a bad length or a CRC mismatch, and MK53ModbusError
        when the chamber replies with an exception frame.
        """
        self._flush_input()
        try:
            self._instr.write_raw(req)
            head = bytes(self._instr.read_bytes(3))
            func, third = head[1], head[2]
            if func & 0x80:                         # exception frame
                tail_len = 2
            elif func in (self._FC_READ, 0x04):     # read reply
                if expected_payload is not None and third != expected_payload:
                    raise MK53CommError(
                        f'Unexpected byte count {third} in read reply '
                        f'(expected {expected_payload})')
                tail_len = third + 2
            else:                                   # write echo
                tail_len = 5
            frame = head + bytes(self._instr.read_bytes(tail_len))
        except pyvisa.errors.VisaIOError as exc:
            raise MK53CommError(f'Serial I/O failed: {exc}') from exc

        crc_recv, = struct.unpack('<H', frame[-2:])
        crc_calc  = self._crc16(frame[:-2])
        if crc_recv != crc_calc:
            raise MK53CommError(
                f'CRC mismatch in reply '
                f'(received 0x{crc_recv:04X}, calculated 0x{crc_calc:04X})'
            )
        if frame[0] != self.slave_address:
            raise MK53CommError(
                f'Reply from slave {frame[0]}, expected {self.slave_address}')
        if func & 0x80:
            if (func & 0x7F) != req[1]:
                raise MK53CommError(
                    f'Exception frame for function 0x{func & 0x7F:02X}, '
                    f'request was 0x{req[1]:02X}')
            desc = self._ERROR_CODES.get(third, 'Unknown')
            raise MK53ModbusError(f'Modbus error {third}: {desc}')
        return frame

    def _with_retries(self, fn, retries: int = None):
        """Run fn(), retrying on MK53CommError; other errors pass through."""
        attempts = max(1, self.retries if retries is None else retries)
        last_exc = None
        for _ in range(attempts):
            try:
                return fn()
            except MK53CommError as exc:
                last_exc = exc
        raise MK53CommError(
            f'failed after {attempts} attempts: {last_exc}'
        ) from last_exc

    def _read_registers(self, addr: int, n_words: int,
                        retries: int = None) -> list:
        """
        Send a Modbus read request (FC 0x03) and return a list of uint16 words.
        Raises MK53CommError after repeated comm failures and MK53ModbusError
        if the chamber rejects the request.
        """
        req = self._make_read_request(addr, n_words)

        def once():
            resp = self._transact(req, expected_payload=n_words * 2)
            return self._parse_read_response(resp)

        return self._with_retries(once, retries)

    def _write_registers(self, addr: int, words: tuple, retries: int = None):
        """
        Send a Modbus write request (FC 0x10) for a tuple of uint16 words.
        Verifies the echo response from the chamber. Writing a fixed value is
        idempotent, so comm errors are retried like reads.
        """
        req = self._make_write_request(addr, words)

        def once():
            resp = self._transact(req)
            resp_addr, resp_n_words = self._parse_write_response(resp)
            if resp_addr != addr or resp_n_words != len(words):
                raise MK53CommError(
                    f'Write echo mismatch: expected addr=0x{addr:04X} '
                    f'n={len(words)}, got addr=0x{resp_addr:04X} '
                    f'n={resp_n_words}'
                )

        self._with_retries(once, retries)

    # ------------------------------------------------------------------
    # Frame builders
    # ------------------------------------------------------------------

    def _make_read_request(self, addr: int, n_words: int) -> bytes:
        msg = struct.pack('>BBHH', self.slave_address, self._FC_READ,
                          addr, n_words)
        return msg + struct.pack('<H', self._crc16(msg))

    def _make_write_request(self, addr: int, words: tuple) -> bytes:
        n_words = len(words)
        msg = struct.pack('>BBHHB',
                          self.slave_address, self._FC_WRITE,
                          addr, n_words, n_words * 2)
        for word in words:
            msg += struct.pack('>H', word)
        return msg + struct.pack('<H', self._crc16(msg))

    # ------------------------------------------------------------------
    # Frame parsers  (CRC, slave and exception checks are in _transact)
    # ------------------------------------------------------------------

    def _parse_read_response(self, data: bytes) -> list:
        """Extract the uint16 words from a CRC-checked read reply frame."""
        if len(data) < 5:
            raise MK53CommError(f'Read response too short ({len(data)} bytes)')
        n_bytes = data[2]
        if n_bytes & 1:
            raise MK53CommError('Odd byte count in read response')
        if len(data) != 5 + n_bytes:
            raise MK53CommError(
                f'Read response length {len(data)} != {5 + n_bytes}')
        return [struct.unpack('>H', data[3 + i*2 : 5 + i*2])[0]
                for i in range(n_bytes >> 1)]

    def _parse_write_response(self, data: bytes) -> tuple:
        """Return (addr, n_words) from a CRC-checked write echo frame."""
        if len(data) != 8:
            raise MK53CommError(f'Write response length {len(data)} != 8')
        _, func, addr, n_words = struct.unpack('>BBHH', data[:6])
        if func != self._FC_WRITE:
            raise MK53CommError(
                f'Unexpected function code 0x{func:02X} in write response')
        return addr, n_words

    # ------------------------------------------------------------------
    # Float encoding / decoding  (IEEE-754, word-swapped)
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_float(value: float) -> tuple:
        """
        Encode a Python float as two uint16 words in Binder word-swapped order.
        Big-endian IEEE-754 → split into [high_word, low_word] → stored as
        (low_word, high_word) to match the chamber's byte order.
        """
        high, low = struct.unpack('>HH', struct.pack('>f', value))
        return (low, high)    # word-swapped

    @staticmethod
    def _decode_float(words: list) -> float:
        """
        Decode two uint16 words (word-swapped) back to a Python float.
        """
        return struct.unpack('>f', struct.pack('>HH', words[1], words[0]))[0]

    # ------------------------------------------------------------------
    # CRC-16  (Modbus standard: poly=0xA001, init=0xFFFF)
    # ------------------------------------------------------------------

    @staticmethod
    def _crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                lsb = crc & 1
                crc >>= 1
                if lsb:
                    crc ^= 0xA001
        return crc


# ---------------------------------------------------------------------------
# Usage example
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    VISA_ADDR = 'ASRL3::INSTR'   # change to your COM port

    with MK53(
        VISA_ADDR,
        slave_address=1,
        min_temp=-40.0,
        max_temp=180.0,
    ) as chamber:
        print(chamber.identify())

        print(f'Mode          : {chamber.get_mode()}')
        print(f'Actual temp   : {chamber.get_temperature():.2f} °C')
        print(f'Setpoint      : {chamber.get_temperature_setpoint():.2f} °C')

        # Ramp to 85 °C at 5 °C/min, then wait up to 15 min for stability
        chamber.ramp_to(target_c=85.0, rate_c_per_min=5.0)
        stable = chamber.wait_for_stability(
            setpoint_c=85.0,
            tolerance_c=0.5,
            stable_seconds=60,
            timeout_seconds=900,
        )
        print(f'Stable at 85 °C: {stable}')
        print(f'Final temp: {chamber.get_temperature():.2f} °C')
