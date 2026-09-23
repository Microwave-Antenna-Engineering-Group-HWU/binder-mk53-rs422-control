"""
Small window to watch and set the MK 53 chamber temperature.

    python chamber_gui.py                 find the FTDI adapter, address 1
    python chamber_gui.py --port COM5 --slave 2
    python chamber_gui.py --demo          simulated chamber, no hardware needed
    python chamber_gui.py --no-logo       hide the logo

The window shows the actual temperature, refreshed every second, with the
current setpoint, the operating mode and a trend of the last five minutes.
Type a temperature and press Set (or Enter) to change the setpoint.

A background thread owns the serial port, so the window never freezes while
the chamber answers. Like set_temperature.py, it asks before writing while a
temperature programme is running, and it does not start an idle chamber. If
the USB adapter is unplugged it keeps trying and carries on when it returns.
Run with the project's virtual environment (see the README).
"""

import argparse
import collections
import math
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from mk53_driver import MK53, MK53Error, confirm_setpoint, resolve_port

DEFAULT_PORT = 'auto'             # find the FTDI USB adapter, or name it, e.g. COM5
DEFAULT_SLAVE = 1
DEFAULT_SETPOINT = 22.0
MIN_C, MAX_C = -40.0, 180.0       # chamber range (E2.1 manual)

POLL_S = 1.0          # actual temperature is read this often
SLOW_EVERY = 5        # setpoint and mode are read on every 5th poll
HISTORY_S = 300       # trend window in seconds
STALE_S = 3.0         # grey out the reading if nothing arrived for this long
READBACK_TOLERANCE_C = 0.05

PLOT_W, PLOT_H = 520, 180        # at 96 dpi; scaled up on high-DPI screens
LOGO_H = 44                      # logo height in pixels at 96 dpi
LOGO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets',
                         'heriot_watt_logo.png')


def load_logo(height_px):
    """Heriot-Watt logo scaled to the given height, or None if it cannot be loaded."""
    try:
        from PIL import Image, ImageTk
        img = Image.open(LOGO_FILE)
        width = round(img.width * height_px / img.height)
        return ImageTk.PhotoImage(img.resize((width, height_px), Image.LANCZOS))
    except Exception:
        return None       # no Pillow or no file: the window works without the logo


class DemoChamber:
    """Simulated chamber: the temperature drifts towards the setpoint."""

    def __init__(self):
        self._temp, self._setpoint, self._mode = 23.5, 25.0, ['manual']
        self._last = time.monotonic()

    def _advance(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        self._temp += (self._setpoint - self._temp) * min(1.0, dt / 90.0)

    def get_temperature(self):
        self._advance()
        return self._temp + random.uniform(-0.02, 0.02)

    def get_temperature_setpoint(self):
        return self._setpoint

    def get_mode(self):
        return list(self._mode)

    def set_temperature(self, value):
        if not MIN_C <= value <= MAX_C:
            raise ValueError(f'{value} °C is outside {MIN_C:g} to {MAX_C:g} °C')
        self._setpoint = value

    def close(self):
        pass


class Worker(threading.Thread):
    """Owns the chamber. Polls once a second and runs set commands in order.

    If the USB adapter is unplugged or the port fails, it keeps trying to reopen
    the port and carries on when the adapter comes back.

    Messages for the window go into the results queue as tuples:
    ('connected',), ('open_failed', text), ('temp', celsius, unix_time),
    ('state', setpoint, mode), ('comm_error', text), ('reconnected',),
    ('set_ok', target, readback), ('set_failed', text), ('needs_force', target),
    ('closed',)
    """

    def __init__(self, open_chamber, results):
        super().__init__(daemon=True)
        self._open = open_chamber
        self.results = results
        self.commands = queue.Queue()
        self.chamber = None
        self.port_failed = False

    def stop(self):
        self.commands.put(('stop',))

    def set_temperature(self, value, force=False):
        self.commands.put(('set', value, force))

    def run(self):
        try:
            self.chamber = self._open()
        except Exception as exc:
            self.results.put(('open_failed', str(exc)))
            return
        self.results.put(('connected',))
        try:
            self._loop()
        finally:
            self._close_chamber()
            self.results.put(('closed',))

    def _close_chamber(self):
        try:
            if self.chamber is not None:
                self.chamber.close()
        except Exception:
            pass

    def _loop(self):
        next_poll = time.monotonic()
        polls = 0
        slow_due = True
        while True:
            try:
                cmd = self.commands.get(timeout=max(0.0, next_poll - time.monotonic()))
            except queue.Empty:
                cmd = None
            if cmd is not None:
                if cmd[0] == 'stop':
                    return
                if cmd[0] == 'set':
                    slow_due |= self._do_set(cmd[1], cmd[2])
                continue
            next_poll = max(next_poll + POLL_S, time.monotonic())
            if self.port_failed:
                if not self._reopen():
                    continue
                slow_due = True                  # refresh setpoint and mode after a reconnect
            self._poll(slow_due or polls % SLOW_EVERY == 0)
            slow_due = False
            polls += 1

    def _reopen(self):
        self._close_chamber()
        try:
            self.chamber = self._open()
        except Exception as exc:
            self.results.put(('comm_error', f'Waiting for the adapter: {exc}'))
            return False
        self.port_failed = False
        self.results.put(('reconnected',))
        return True

    def _note_failure(self, exc):
        self.results.put(('comm_error', str(exc)))
        if not isinstance(exc, (MK53Error, ValueError)):
            self.port_failed = True              # a port problem, not just a silent chamber

    def _poll(self, slow):
        try:
            self.results.put(('temp', self.chamber.get_temperature(), time.time()))
            if slow:
                setpoint = self.chamber.get_temperature_setpoint()
                self.results.put(('state', setpoint, self.chamber.get_mode()))
        except Exception as exc:
            self._note_failure(exc)

    def _do_set(self, value, force):
        """Returns True if the window should refresh setpoint and mode."""
        if self.port_failed:
            self.results.put(('set_failed', 'The port is not available. Waiting for the adapter.'))
            return False
        try:
            if 'auto' in self.chamber.get_mode() and not force:
                self.results.put(('needs_force', value))
                return False
            self.chamber.set_temperature(value)
            readback = confirm_setpoint(self.chamber, value, tolerance_c=READBACK_TOLERANCE_C)
        except Exception as exc:
            self.results.put(('set_failed', str(exc)))
            if not isinstance(exc, (MK53Error, ValueError)):
                self.port_failed = True
            return False
        if abs(readback - value) > READBACK_TOLERANCE_C:
            self.results.put(('set_failed',
                              f'Wrote {value:.2f} °C but the chamber reports {readback:.2f} °C'))
            return True
        self.results.put(('set_ok', value, readback))
        return True


class App:
    def __init__(self, root, demo=False, port=DEFAULT_PORT, slave=DEFAULT_SLAVE,
                 setpoint=DEFAULT_SETPOINT, show_logo=True):
        self.root, self.demo = root, demo
        self.results = queue.Queue()
        self.worker = None
        self.closing = False
        self.history = collections.deque(maxlen=int(HISTORY_S * 1.5))
        self.setpoint = None
        self.mode = None
        self.last_ok = None
        self.temp_now = None
        self.destroyed = False
        self.scale = max(1.0, root.winfo_fpixels('1i') / 96.0)
        self.plot_w, self.plot_h = round(PLOT_W * self.scale), round(PLOT_H * self.scale)

        root.title('MK 53 chamber' + ('  (demo)' if demo else ''))
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=12)
        frame.grid()

        header = tk.Frame(frame, bg='white')
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        self.logo = load_logo(round(LOGO_H * self.scale)) if show_logo else None   # keep a reference
        if self.logo is not None:
            tk.Label(header, image=self.logo, bg='white').pack(side='left', padx=8, pady=6)
        tk.Label(header, text='MK 53 climate chamber', bg='white', fg='#444444',
                 font=('Segoe UI', 14)).pack(side='right', padx=12)

        conn = ttk.Frame(frame)
        conn.grid(row=1, column=0, sticky='ew')
        ttk.Label(conn, text='Port').pack(side='left')
        self.port_var = tk.StringVar(value=port)
        self.port_entry = ttk.Entry(conn, textvariable=self.port_var, width=14)
        self.port_entry.pack(side='left', padx=(4, 12))
        ttk.Label(conn, text='Address').pack(side='left')
        self.slave_var = tk.StringVar(value=str(slave))
        self.slave_box = ttk.Spinbox(conn, from_=1, to=255, width=5,
                                     textvariable=self.slave_var)
        self.slave_box.pack(side='left', padx=(4, 12))
        self.connect_btn = ttk.Button(conn, text='Connect', command=self.toggle_connection)
        self.connect_btn.pack(side='left')

        self.temp_label = tk.Label(frame, text='--.-- °C', font=('Segoe UI', 40, 'bold'),
                                   fg='#888888')
        self.temp_label.grid(row=2, column=0, pady=(10, 0))
        ttk.Label(frame, text='Actual temperature').grid(row=3, column=0)

        self.info_var = tk.StringVar(value='Setpoint --   |   Mode --   |   Updated --')
        ttk.Label(frame, textvariable=self.info_var).grid(row=4, column=0, pady=(6, 10))

        setter = ttk.Frame(frame)
        setter.grid(row=5, column=0)
        ttk.Label(setter, text='Set temperature (°C)').pack(side='left')
        self.target_var = tk.StringVar(value=f'{setpoint:g}')
        self.target_entry = ttk.Entry(setter, textvariable=self.target_var, width=8)
        self.target_entry.pack(side='left', padx=8)
        self.set_btn = ttk.Button(setter, text='Set', command=self.on_set)
        self.set_btn.pack(side='left')
        self.target_entry.bind('<Return>', lambda _e: self.on_set())

        self.plot = tk.Canvas(frame, width=self.plot_w, height=self.plot_h, bg='white',
                              highlightthickness=1, highlightbackground='#cccccc')
        self.plot.grid(row=6, column=0, pady=10)

        self.status_var = tk.StringVar(value='Not connected')
        self.status = tk.Label(frame, textvariable=self.status_var, anchor='w', fg='#444444')
        self.status.grid(row=7, column=0, sticky='ew')

        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.draw_plot()
        self.root.after(100, self.drain)
        self.connect()

    # ---- connection ---------------------------------------------------------

    def toggle_connection(self):
        if self.worker is None:
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        if self.worker is not None or self.closing:
            return
        try:
            slave = int(self.slave_var.get())
            if not 1 <= slave <= 255:
                raise ValueError
        except ValueError:
            self.say('The address must be a whole number from 1 to 255', error=True)
            return
        port = self.port_var.get().strip()
        if self.demo:
            factory = DemoChamber
        else:
            def factory():
                # resolved on every (re)open, so an adapter that comes back on a
                # different COM number is still found
                return MK53(resolve_port(port), slave_address=slave,
                            timeout_ms=1000, retries=1)
        self.worker = Worker(factory, self.results)
        self.worker.start()
        self.connect_btn.config(text='Disconnect')
        self.port_entry.state(['disabled'])
        self.slave_box.state(['disabled'])
        self.say('Connecting ...')

    def disconnect(self):
        if self.worker is not None:
            self.connect_btn.config(state='disabled')     # re-enabled on 'closed'
            self.worker.stop()
            self.say('Disconnecting ...')

    def reset_connection_ui(self):
        self.worker = None
        self.connect_btn.config(text='Connect', state='normal')
        self.port_entry.state(['!disabled'])
        self.slave_box.state(['!disabled'])
        self.last_ok = None
        self.refresh_labels()

    # ---- setting the temperature -------------------------------------------

    def on_set(self):
        if self.worker is None:
            self.say('Connect first', error=True)
            return
        try:
            value = float(self.target_var.get().replace(',', '.'))
        except ValueError:
            self.say('Type the temperature as a number, for example 22', error=True)
            return
        if not math.isfinite(value) or not MIN_C <= value <= MAX_C:
            self.say(f'The chamber works from {MIN_C:g} to {MAX_C:g} °C', error=True)
            return
        self.say(f'Setting {value:.2f} °C ...')
        self.worker.set_temperature(value)

    # ---- messages from the worker ------------------------------------------

    def drain(self):
        self.root.after(100, self.drain)       # scheduled first so a dialog cannot stall it
        try:
            while True:
                self.handle(self.results.get_nowait())
        except queue.Empty:
            pass
        if not self.destroyed:
            self.refresh_labels()

    def handle(self, msg):
        kind = msg[0]
        if kind == 'connected':
            self.say('Connected')
        elif kind == 'temp':
            self.last_ok = msg[2]
            self.history.append((msg[2], msg[1]))
            self.temp_now = msg[1]
            self.draw_plot()
        elif kind == 'state':
            self.setpoint, self.mode = msg[1], msg[2]
        elif kind == 'comm_error':
            self.say(f'No reply from the chamber: {msg[1]}', error=True)
        elif kind == 'reconnected':
            self.say('Adapter found again, reconnected', good=True)
        elif kind == 'set_ok':
            self.say(f'Setpoint is now {msg[2]:.2f} °C', good=True)
        elif kind == 'set_failed':
            self.say(f'Could not set the temperature: {msg[1]}', error=True)
            messagebox.showerror('Could not set the temperature', msg[1])
        elif kind == 'needs_force':
            self.say('A temperature programme is running')
            if messagebox.askyesno(
                    'Programme running',
                    'A temperature programme is running (auto mode). A manual '
                    'setpoint may not take effect and could disturb the run.\n\n'
                    f'Set {msg[1]:.2f} °C anyway?'):
                self.worker.set_temperature(msg[1], force=True)
            else:
                self.say('Not changed')
        elif kind == 'open_failed':
            self.say(f'Could not connect: {msg[1]}', error=True)
            messagebox.showerror('Could not connect', msg[1])
            self.reset_connection_ui()
        elif kind == 'closed':
            self.say('Disconnected')
            self.reset_connection_ui()
            if self.closing:
                self.destroyed = True
                self.root.destroy()

    # ---- drawing ------------------------------------------------------------

    def say(self, text, error=False, good=False):
        self.status_var.set(text)
        self.status.config(fg='#b00020' if error else '#1b7f3b' if good else '#444444')

    def refresh_labels(self):
        stale = self.last_ok is None or time.time() - self.last_ok > STALE_S
        if self.last_ok is None:
            self.temp_label.config(text='--.-- °C', fg='#888888')
        else:
            self.temp_label.config(text=f'{self.temp_now:.2f} °C',
                                   fg='#888888' if stale else '#111111')
        sp = '--' if self.setpoint is None else f'{self.setpoint:.2f} °C'
        mode = '--' if not self.mode else ', '.join(self.mode)
        updated = '--' if self.last_ok is None else time.strftime('%H:%M:%S', time.localtime(self.last_ok))
        self.info_var.set(f'Setpoint {sp}   |   Mode {mode}   |   Updated {updated}')

    def draw_plot(self):
        c = self.plot
        c.delete('all')
        k = self.scale
        left, right = round(52 * k), self.plot_w - round(12 * k)
        top, bottom = round(12 * k), self.plot_h - round(24 * k)
        small = ('Segoe UI', 8)
        c.create_rectangle(left, top, right, bottom, outline='#bbbbbb')
        now = time.time()
        points = [(t, v) for t, v in self.history if now - t <= HISTORY_S]
        if len(points) < 2:
            c.create_text((left + right) / 2, (top + bottom) / 2,
                          text='Waiting for data', fill='#888888')
            return
        values = [v for _, v in points]
        if self.setpoint is not None:
            values.append(self.setpoint)
        low, high = min(values), max(values)
        if high - low < 2.0:
            mid = (high + low) / 2
            low, high = mid - 1.0, mid + 1.0
        pad = (high - low) * 0.08
        low, high = low - pad, high + pad

        def x_of(t):
            return right - (now - t) / HISTORY_S * (right - left)

        def y_of(v):
            return bottom - (v - low) / (high - low) * (bottom - top)

        if self.setpoint is not None:
            y = y_of(self.setpoint)
            c.create_line(left, y, right, y, fill='#d9822b', dash=(4, 3))
            c.create_text(right - 4, y - round(9 * k), text=f'setpoint {self.setpoint:.1f}',
                          anchor='e', fill='#d9822b', font=small)
        coords = [xy for t, v in points for xy in (x_of(t), y_of(v))]
        c.create_line(*coords, fill='#1f6fb2', width=2)
        c.create_text(left - 4, top + round(4 * k), text=f'{high:.1f}', anchor='e', font=small)
        c.create_text(left - 4, bottom - round(4 * k), text=f'{low:.1f}', anchor='e', font=small)
        c.create_text(left, bottom + round(12 * k), text=f'{HISTORY_S / 60:g} min ago', anchor='w', font=small)
        c.create_text(right, bottom + round(12 * k), text='now', anchor='e', font=small)

    # ---- closing ------------------------------------------------------------

    def on_close(self):
        if self.worker is None:
            self.destroyed = True
            self.root.destroy()
            return
        self.closing = True
        self.worker.stop()
        self.root.after(2500, self.root.destroy)          # give up waiting after 2.5 s


def main():
    ap = argparse.ArgumentParser(description='MK 53 temperature window')
    ap.add_argument('--port', default=DEFAULT_PORT,
                    help='COM port or VISA resource, e.g. COM5 (default: find the FTDI adapter)')
    ap.add_argument('--slave', type=int, default=DEFAULT_SLAVE,
                    help=f'controller address (default {DEFAULT_SLAVE})')
    ap.add_argument('--setpoint', type=float, default=DEFAULT_SETPOINT,
                    help=f'value shown in the set box (default {DEFAULT_SETPOINT:g})')
    ap.add_argument('--demo', action='store_true', help='simulated chamber, no hardware')
    ap.add_argument('--no-logo', action='store_true', help='do not show the logo')
    args = ap.parse_args()

    try:                                   # sharper text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root, demo=args.demo, port=args.port, slave=args.slave, setpoint=args.setpoint,
        show_logo=not args.no_logo)
    root.mainloop()


if __name__ == '__main__':
    main()
