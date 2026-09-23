"""
First-time connection and sanity check for the Binder MK53 climate chamber.

By default this script is READ-ONLY. It verifies:
1. The serial port opens
2. The chamber answers a register read (actual temperature)
3. The active operating mode is readable
4. The current setpoint is readable

Optional, only when you ask for them:
--write       write the current setpoint back and read it back (changes the
              chamber's manual/basic setpoint registers; skipped in auto mode)
--stability   wait up to 15 min for the chamber to reach its setpoint

Hardware requirement
────────────────────
USB-to-RS422 adapter connected to the MK53 RS-422 port (DB25) on the lateral
(left side) control panel. The controller address (1 to 30)
is set in the controller menu: Instrument data > Address.
NOTE: the Modbus protocol used by mk53_driver.py is not documented in the
E2.1 manual, so a failure in step 2 may mean the protocol assumption is wrong,
not just a wiring problem.

Run (from the Code folder):
    python check_connection.py
    python check_connection.py --port COM5 --slave 1 --write --stability

The exit code is 0 on PASS and 1 on FAIL.
"""

import argparse
import sys
import time

try:
    from mk53_driver import MK53, MK53CommError, MK53ModbusError, resolve_port
except ImportError as exc:
    print(f'[FAIL] Could not import the driver: {exc}')
    print('       Install the dependencies:  pip install pyvisa pyvisa-py pyserial')
    sys.exit(1)

# ── Defaults (override on the command line) ────────────────────────────────
VISA_ADDR  = 'auto'          # find the FTDI USB adapter, or name it, e.g. COM5
SLAVE_ADDR = 1                # controller address set in the chamber menu
MIN_TEMP   = -40.0            # °C  safety limit
MAX_TEMP   = 180.0            # °C  safety limit

# ── simple colour-free pass/fail banner ───────────────────────────────────
PASS = '[PASS]'
FAIL = '[FAIL]'
INFO = '[INFO]'
SKIP = '[SKIP]'


def section(title: str):
    print(f'\n{"─" * 55}')
    print(f'  {title}')
    print('─' * 55)


def comm_hint(exc: Exception) -> str:
    """Point at the likely cause for a failed register access."""
    if isinstance(exc, MK53ModbusError):
        return ('The chamber answered but rejected the request '
                '(register address or access), so the protocol is at least partly right')
    if isinstance(exc, MK53CommError):
        return ('No valid reply: check baud/wiring (RxD/TxD swap), the controller '
                'address in the chamber menu, and that the chamber really speaks '
                'this protocol')
    return ''


def run(port: str, slave: int, do_write: bool, do_stability: bool) -> int:
    # box-drawing characters fail on a cp1252 console or redirected output
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    print('=' * 55)
    print('  Binder MK53 — First-Time Connection Test')
    print('=' * 55)
    print(f'{INFO} VISA resource : {port}')
    print(f'{INFO} Slave address : {slave}')
    print(f'{INFO} Mode          : read-only'
          + (' + write round-trip' if do_write else '')
          + (' + stability wait' if do_stability else ''))

    errors = []

    # ── 1. Open connection ─────────────────────────────────────────────────
    section('Step 1 — Open serial connection')
    try:
        port = resolve_port(port)
        chamber = MK53(port, slave_address=slave,
                       min_temp=MIN_TEMP, max_temp=MAX_TEMP)
        print(f'{PASS} Port opened successfully')
    except Exception as exc:
        print(f'{FAIL} Could not open port: {exc}')
        print('\nCheck:')
        print('  • USB-to-RS422 adapter is plugged in')
        print(f'  • COM port matches Device Manager (currently: {port})')
        print('  • No other program has the port open')
        return 1

    with chamber:

        # ── 2. Read actual temperature ─────────────────────────────────────
        section('Step 2 — Read actual temperature')
        try:
            temp = chamber.get_temperature()
            print(f'{PASS} Actual temperature : {temp:.2f} °C')
            if not -50 <= temp <= 200:
                print(f'{FAIL} Temperature {temp:.2f}°C is outside expected physical range '
                      f'— check wiring or slave address')
                errors.append('Temperature out of physical range')
        except Exception as exc:
            print(f'{FAIL} Could not read temperature: {exc}')
            hint = comm_hint(exc)
            if hint:
                print(f'  Hint: {hint}')
            errors.append(f'Temperature read failed: {exc}')

        # ── 3. Read operating mode ─────────────────────────────────────────
        section('Step 3 — Read operating mode')
        mode = None
        try:
            mode = chamber.get_mode()
            print(f'{PASS} Operating mode : {mode}')
        except Exception as exc:
            print(f'{FAIL} Could not read mode register: {exc}')
            errors.append(f'Mode read failed: {exc}')

        # ── 4. Read current setpoint ───────────────────────────────────────
        section('Step 4 — Read temperature setpoint')
        setpt = None
        try:
            setpt = chamber.get_temperature_setpoint()
            print(f'{PASS} Current setpoint : {setpt:.2f} °C')
        except Exception as exc:
            print(f'{FAIL} Could not read setpoint: {exc}')
            errors.append(f'Setpoint read failed: {exc}')

        # ── 5. Write setpoint and read back (opt-in) ──────────────────────
        section('Step 5 — Setpoint write round-trip')
        if not do_write:
            print(f'{SKIP} Not requested (use --write)')
        elif setpt is None or mode is None:
            print(f'{SKIP} Needs a successful mode and setpoint read first')
        elif 'auto' in mode:
            print(f'{SKIP} Chamber is running a program (auto mode); '
                  f'not touching the setpoint registers')
        else:
            try:
                chamber.set_temperature(setpt)
                time.sleep(0.5)                     # small settling delay
                readback = chamber.get_temperature_setpoint()
                delta = abs(readback - setpt)

                if delta < 0.1:                     # float round-trip tolerance
                    print(f'{PASS} Wrote {setpt:.2f}°C, read back {readback:.2f}°C '
                          f'(Δ = {delta:.3f}°C)')
                else:
                    print(f'{FAIL} Write/read mismatch: wrote {setpt:.2f}°C, '
                          f'got {readback:.2f}°C')
                    errors.append('Setpoint write round-trip mismatch')
            except ValueError as exc:
                # set_temperature raises ValueError for out-of-range values
                print(f'{FAIL} Safety limit rejected the write: {exc}')
                errors.append(f'Setpoint write rejected: {exc}')
            except Exception as exc:
                print(f'{FAIL} Setpoint write failed: {exc}')
                errors.append(f'Setpoint write failed: {exc}')

        # ── 6. Wait for temperature stability (opt-in) ────────────────────
        section('Step 6 — Wait for temperature stability')
        if not do_stability:
            print(f'{SKIP} Not requested (use --stability)')
        elif setpt is None:
            print(f'{SKIP} Needs a successful setpoint read first')
        else:
            try:
                print(f'{INFO} Waiting for chamber to reach {setpt:.2f}°C '
                      f'(±0.5°C for 60 s, timeout 15 min) ...')
                stable = chamber.wait_for_stability(
                    setpoint_c=setpt,
                    tolerance_c=0.5,
                    stable_seconds=60,
                    timeout_seconds=900,
                    poll_interval_s=5.0,
                )
                actual = chamber.get_temperature()
                if stable:
                    print(f'{PASS} Stable at {actual:.2f}°C')
                else:
                    print(f'{FAIL} Timed out — actual temp is {actual:.2f}°C, '
                          f'setpoint is {setpt:.2f}°C')
                    errors.append('Temperature stability timeout')
            except Exception as exc:
                print(f'{FAIL} Stability wait failed: {exc}')
                errors.append(f'Stability wait failed: {exc}')

        # ── 7. Identify (composed summary) ────────────────────────────────
        section('Step 7 — Instrument summary')
        print(chamber.identify())

    # ── Final result ───────────────────────────────────────────────────────
    print('\n' + '=' * 55)
    if errors:
        print(f'  RESULT: FAIL  ({len(errors)} error(s))')
        for e in errors:
            print(f'  • {e}')
    else:
        print('  RESULT: PASS  — MK53 is communicating correctly')
    print('=' * 55)
    return 1 if errors else 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Binder MK53 connection check')
    ap.add_argument('--port', default=VISA_ADDR,
                    help='COM port or VISA resource, e.g. COM5 (default: find the FTDI adapter)')
    ap.add_argument('--slave', type=int, default=SLAVE_ADDR,
                    help=f'controller address 1-30 (default {SLAVE_ADDR})')
    ap.add_argument('--write', action='store_true',
                    help='also do a setpoint write/read-back round-trip')
    ap.add_argument('--stability', action='store_true',
                    help='also wait for the temperature to stabilise')
    args = ap.parse_args()
    sys.exit(run(args.port, args.slave, args.write, args.stability))
