"""
Quick read-only probe for the MK53 RS-422 link.

It sends the "read actual temperature" request at each baud rate, parity and
controller address and reports every byte that comes back, even garbage. Nothing is ever
written to the chamber. Use it after each wiring change.

    python probe_link.py                                  scan 9600 and 19200, addresses 1 to 30
    python probe_link.py --bauds 9600 --addresses 1
    python probe_link.py --parity N,E,O                   also try even and odd parity
    python probe_link.py --loopback                       adapter test, no chamber needed
    python probe_link.py --port COM5 ...                  use a named port instead of the FTDI adapter

Loopback: disconnect the adapter from the chamber and wire its TX+ to RX+ and
TX- to RX-. Bytes sent should come straight back. This proves the adapter, its
driver and the COM port, and shows which terminals are which.

Exit code 0 if any bytes were received, 1 if none.
"""

import argparse
import sys
import time

import serial

from mk53_driver import MK53, resolve_port


def parse_range(text):
    out = []
    for part in text.split(','):
        if '-' in part:
            lo, hi = part.split('-')
            out += range(int(lo), int(hi) + 1)
        else:
            out.append(int(part))
    return out


def request(addr):
    m = MK53.__new__(MK53)
    m.slave_address = addr
    return m._make_read_request(MK53.ADDR_CURTEMP, 2)


def loopback(ser):
    pattern = bytes([0x55, 0xAA, 0x01, 0x02, 0x03, 0xFE, 0xFF, 0x00])
    ser.reset_input_buffer()
    ser.write(pattern)
    time.sleep(0.05)
    back = ser.read(len(pattern) * 2)
    print(f'sent     : {pattern.hex(" ")}')
    print(f'received : {back.hex(" ") if back else "(nothing)"}')
    if back == pattern:
        print('LOOPBACK PASS: the adapter, its driver and the COM port work.')
        return 0
    if back:
        print('LOOPBACK PARTIAL: bytes came back but not the same ones. '
              'Check the + and - pairs and the adapter mode switch.')
        return 0
    print('LOOPBACK FAIL: nothing came back. Check the loopback wires, the '
          'RS422 or RS485 mode switch and any termination setting on the adapter.')
    return 1


def scan(ser, bauds, parities, addresses):
    replies = 0
    for parity in parities:
        ser.parity = parity
        for baud in bauds:
            ser.baudrate = baud
            got = []
            for addr in addresses:
                ser.reset_input_buffer()
                ser.write(request(addr))
                time.sleep(0.02)
                data = ser.read(64)
                if data:
                    got.append((addr, data))
            replies += len(got)
            print(f'8{parity}1 baud {baud:>6}: {len(got)} of {len(addresses)} '
                  f'addresses replied')
            for addr, data in got[:10]:
                print(f'    address {addr}: {data.hex(" ")}')
    return 0 if replies else 1


def main():
    ap = argparse.ArgumentParser(description='Read-only MK53 link probe')
    ap.add_argument('--port', default='auto',
                    help='COM port, e.g. COM5 (default: find the FTDI adapter)')
    ap.add_argument('--bauds', default='9600,19200',
                    help='comma list (default 9600,19200)')
    ap.add_argument('--parity', default='N',
                    help='comma list of N, E, O (default N)')
    ap.add_argument('--addresses', default='1-30',
                    help='addresses, e.g. 1-30 or 1,2,5 (default 1-30)')
    ap.add_argument('--timeout', type=float, default=0.3,
                    help='seconds to wait for a reply (default 0.3)')
    ap.add_argument('--loopback', action='store_true',
                    help='adapter loopback test instead of scanning')
    args = ap.parse_args()

    port = resolve_port(args.port)                # ASRL12::INSTR form
    port = 'COM' + port[4:].split(':')[0]         # pyserial wants COM12

    with serial.Serial(port, 9600, bytesize=8, parity='N', stopbits=1,
                       timeout=args.timeout) as ser:
        if args.loopback:
            sys.exit(loopback(ser))
        bauds = [int(b) for b in args.bauds.split(',')]
        parities = [x.strip().upper() for x in args.parity.split(',')]
        sys.exit(scan(ser, bauds, parities, parse_range(args.addresses)))


if __name__ == '__main__':
    main()
