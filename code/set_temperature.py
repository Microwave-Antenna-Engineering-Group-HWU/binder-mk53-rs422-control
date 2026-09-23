"""
Set the MK 53 chamber temperature (default 22 degC).

    python set_temperature.py                    set 22 degC
    python set_temperature.py 30                 set 30 degC
    python set_temperature.py --dry-run          show the state, write nothing
    python set_temperature.py --wait 15          also wait up to 15 min for stability

The script reads the chamber first and prints what it found. It writes the new
setpoint to the manual and basic setpoint registers, reads it back, and checks
it matches. It does not start the chamber: if the controller is idle, the new
setpoint is stored but the chamber will not regulate until it is started at
the controller. It refuses to run while a temperature programme is active
(auto mode) unless you pass --force.

Exit code 0 on success, 1 on failure, 2 if refused because a programme is running.
Run with the project's virtual environment (see the README).
"""

import argparse
import sys

from mk53_driver import MK53, MK53Error, confirm_setpoint, resolve_port

DEFAULT_TEMP_C = 22.0
DEFAULT_PORT = 'auto'            # find the FTDI USB adapter, or name it, e.g. COM5
DEFAULT_SLAVE = 1                # controller address (menu: Instrument data > Address)
READBACK_TOLERANCE_C = 0.05


def run(chamber, target_c, dry_run=False, force=False, wait_minutes=0.0, out=print):
    """Set the setpoint. Returns 0 on success, 1 on failure, 2 if refused."""
    try:
        actual = chamber.get_temperature()
        setpoint = chamber.get_temperature_setpoint()
        mode = chamber.get_mode()
    except MK53Error as exc:
        out(f'[FAIL] Could not read the chamber: {exc}')
        return 1

    out(f'[INFO] Actual temperature : {actual:.2f} °C')
    out(f'[INFO] Current setpoint   : {setpoint:.2f} °C')
    out(f'[INFO] Operating mode     : {mode}')
    out(f'[INFO] Requested setpoint : {target_c:.2f} °C')

    if 'auto' in mode and not force:
        out('[STOP] A temperature programme is running (auto mode). Setting a '
            'manual setpoint would not take effect and could confuse the run. '
            'Stop the programme at the controller, or pass --force.')
        return 2
    if mode == ['idle']:
        out('[WARN] The controller is idle. The setpoint will be stored but the '
            'chamber will not heat or cool until it is started at the controller.')

    if dry_run:
        out('[DRY RUN] Nothing was written.')
        return 0

    try:
        chamber.set_temperature(target_c)        # checks the safety limits first
        readback = confirm_setpoint(chamber, target_c)
    except (ValueError, MK53Error) as exc:
        out(f'[FAIL] Could not set the temperature: {exc}')
        return 1

    if abs(readback - target_c) > READBACK_TOLERANCE_C:
        out(f'[FAIL] Wrote {target_c:.2f} °C but the chamber reports '
            f'{readback:.2f} °C. Check the operating mode at the controller.')
        return 1
    out(f'[PASS] Setpoint is now {readback:.2f} °C')

    if wait_minutes > 0:
        out(f'[INFO] Waiting up to {wait_minutes:g} min for {target_c:.2f} °C '
            f'(within 0.5 °C for 60 s) ...')
        try:
            stable = chamber.wait_for_stability(
                target_c, tolerance_c=0.5, stable_seconds=60,
                timeout_seconds=int(wait_minutes * 60))
            final = chamber.get_temperature()
        except MK53Error as exc:
            out(f'[FAIL] Lost contact while waiting: {exc}')
            return 1
        if stable:
            out(f'[PASS] Stable at {final:.2f} °C')
        else:
            out(f'[FAIL] Not stable in time. Actual temperature is {final:.2f} °C')
            return 1
    return 0


def main():
    if hasattr(sys.stdout, 'reconfigure'):       # keep the degree sign safe on Windows
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    ap = argparse.ArgumentParser(description='Set the MK 53 chamber temperature')
    ap.add_argument('temperature', nargs='?', type=float, default=DEFAULT_TEMP_C,
                    help=f'target in degC (default {DEFAULT_TEMP_C:g})')
    ap.add_argument('--port', default=DEFAULT_PORT,
                    help='COM port or VISA resource, e.g. COM5 (default: find the FTDI adapter)')
    ap.add_argument('--slave', type=int, default=DEFAULT_SLAVE,
                    help=f'controller address (default {DEFAULT_SLAVE})')
    ap.add_argument('--wait', type=float, default=0.0, metavar='MINUTES',
                    help='wait up to this many minutes for the temperature to stabilise')
    ap.add_argument('--dry-run', action='store_true', help='read only, write nothing')
    ap.add_argument('--force', action='store_true',
                    help='write even if a temperature programme is running')
    args = ap.parse_args()

    try:
        chamber = MK53(resolve_port(args.port), slave_address=args.slave)
    except Exception as exc:
        print(f'[FAIL] Could not open the chamber port: {exc}')
        return 1
    with chamber:
        return run(chamber, args.temperature, args.dry_run, args.force, args.wait)


if __name__ == '__main__':
    sys.exit(main())
