#!/usr/bin/python

"""Clipster - Clipboard manager."""

from __future__ import print_function
from gi.repository import Gtk, Gdk, GLib, GObject
import signal
import argparse
import json
import socket
import os
import errno
import sys
import select
try:
    # py 3.x
    import configparser
except ImportError:
    # py 2.x
    import ConfigParser as configparser


class Clipster(object):
    """Clipboard Manager."""

    def __init__(self, config, stdin):
        self.config = config
        self.stdin = stdin

    def client(self, client_action):
        """Send a signal and (optional) data from STDIN to daemon socket."""

        message = "{0}:{1}:{2}".format(client_action,
                                       self.config.get('clipster',
                                                       'default_selection'),
                                       self.stdin)

        sock_c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_c.connect(self.config.get('clipster', "socket_file"))
        sock_c.sendall(message.encode('utf-8'))
        sock_c.close()

    class Daemon(object):
        """Handles clipboard events, client requests, stores history."""

        def __init__(self, config):
            """Set up clipboard objects and history dict."""
            self.config = config
            self.window = self.p_id = self.c_id = self.sock_s = None
            self.sock_file = self.config.get('clipster', 'socket_file')
            self.primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
            self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            self.boards = {"PRIMARY": [], "CLIPBOARD": []}
            self.hist_file = self.config.get('clipster', 'history_file')
            self.pid_file = self.config.get('clipster', 'pid_file')
            self.max_input = self.config.getint('clipster', 'max_input')

        def keypress_handler(self, widget, event):
            """Handler for selection_widget keypress events."""

            # Hide window if ESC is pressed
            if event.keyval == Gdk.KEY_Escape:
                self.window.hide()

        def selection_handler(self, tree, path, col, board):
            """Handler for selection widget 'select' event."""

            # Get selection
            model, treeiter = tree.get_selection().get_selected()
            data = model[treeiter][0]
            self.update_board(board, data)
            model.clear()
            self.window.hide()

        def selection_widget(self, board):
            """GUI window for selecting items from clipboard history."""

            self.window = Gtk.Dialog(title="Clipster")
            self.window.set_size_request(500,500)

            scrolled = Gtk.ScrolledWindow()

            model = Gtk.ListStore(str)
            for item in self.boards[board][::-1]:
                model.append([item])

            tree = Gtk.TreeView(model)

            # Allow alternating color for rows, if WM theme supports it
            tree.set_rules_hint(True)

            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn("{0} clipboard:".format(board),
                                        renderer, text=0)
            tree.append_column(column)
            # Handle keypresses (looking for escape key)
            self.window.connect("key-press-event", self.keypress_handler)
            # Row is clicked on, or enter pressed
            tree.connect("row-activated", self.selection_handler, board)

            scrolled.add(tree)
            # GtkDialog comes with a vbox already active, so pack into this
            self.window.vbox.pack_start(scrolled, True, True, 0)

            self.window.show_all()

        def read_history_file(self):
            """Read clipboard history from file."""
            try:
                with open(self.hist_file, 'r') as hist_f:
                    self.boards.update(json.load(hist_f))
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    # Not an error if there is no history file
                    pass

        def write_history_file(self):
            """Write clipboard history to file."""

            with open(self.hist_file, 'w') as hist_f:
                json.dump(self.boards, hist_f)

        def update_board(self, board, data):
            """Update a clipboard."""

            getattr(self, board.lower()).set_text(data, -1)

        def update_history(self, board, text):
            """Update the in-memory clipboard history."""

            # If an item already exists in the clipboard, remove it
            if text in self.boards[board]:
                self.boards[board].remove(text)
            self.boards[board].append(text)
            print(self.boards[board])

        def owner_change(self, board, event):
            """Handler for owner-change clipboard events."""

            selection = str(event.selection)
            if selection == "PRIMARY":
                event_id = self.p_id
            else:
                event_id = self.c_id
            # Some apps update primary during mouse drag (chrome)
            # Block at start to prevent repeated triggering
            board.handler_block(event_id)
            # FIXME: this devs hack is a bit verbose. Look instead at
            # gdk_seat_get_pointer -> gdk_device_get_state
            # once GdkSeat is in stable
            # FIXME: Emacs does this with ctrl-space + kb movement.
            # How to deal with this?
            # Something to do with change-owner always being same owner?
            mouse = None
            for dev in self.window.get_display().get_device_manager().list_devices(Gdk.DeviceType.MASTER):
                if dev.get_source() == Gdk.InputSource.MOUSE:
                    mouse = dev
                    break
            while Gdk.ModifierType.BUTTON1_MASK & self.window.get_root_window().get_device_position(mouse)[3]:
                # Do nothing while mouse button is held down (selection drag)
                pass

            # Read clipboard
            text = board.wait_for_text()
            if text:
                self.update_history(selection, text)
            # Unblock event handling
            board.handler_unblock(event_id)
            return text

        def socket_listen(self, sock_s, _):
            """Establish a socket listening for client connections."""

            conn, _ = sock_s.accept()
            conn.setblocking(0)
            data = []
            recv_total = 0
            while True:
                try:
                    recv = conn.recv(8192)
                    if not recv:
                        break
                    data.append(recv.decode('utf-8'))
                    recv_total += len(recv)
                    if recv_total > self.max_input:
                        break
                except socket.error:
                    break
            if data:
                sent = ''.join(data)
                sig, board, content = sent.split(':', 2)
                if sig == "SELECT":
                    self.selection_widget(board)
                elif sig == "BOARD":
                    if content:
                        self.update_board(board, content)
            conn.close()
            return True

        def prepare_files(self):
            """Ensure that all files and sockets used
            by the daemon are available."""

            # check for existing pid_file, and tidy up if appropriate
            try:
                with open(self.pid_file, 'r') as runf_r:
                    pid = int(runf_r.read())
                    try:
                        # Do nothing, but raise an error if no such process
                        os.kill(pid, 0)
                        print("Daemon already running: pid {0}".format(pid))
                        sys.exit(1)
                    except OSError:
                        try:
                            os.unlink(self.pid_file)
                        except IOError as exc:
                            if exc.errno == errno.ENOENT:
                                # File already gone
                                pass
                            else:
                                raise
            except IOError as exc:
                if exc.errno == errno.ENOENT:
                    pass

            # Create pid file
            with open(self.pid_file, 'w') as runf_w:
                runf_w.write(str(os.getpid()))

            # Create the clipster dir if necessary
            try:
                os.makedirs(self.config.get('clipster', 'clipster_dir'))
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    # ok if directory already exists
                    pass

            # Read in history from file
            self.read_history_file()

            # Create the socket
            try:
                os.unlink(self.sock_file)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    pass

            self.sock_s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock_s.setblocking(0)
            self.sock_s.bind(self.sock_file)
            self.sock_s.listen(5)

        def exit(self):
            """Clean up things before exiting."""

            try:
                os.unlink(self.sock_file)
            except OSError:
                print("Warning: Failed to remove socket file: {0}".format(self.sock_file))
            try:
                os.unlink(self.pid_file)
            except OSError:
                print("Warning: Failed to remove run file: {0}".format(self.pid_file))
            self.write_history_file()
            sys.exit(0)

        def run(self):
            """Launch the clipboard manager daemon.
            Listen for clipboard events & client socket connections."""

            # Set up socket, pid file etc
            self.prepare_files()

            # We need to get the display instance from the window
            # for use in obtaining mouse state.
            # POPUP windows can do this without having to first show the window
            self.window = Gtk.Window(type=Gtk.WindowType.POPUP)

            # Handle clipboard changes
            self.p_id = self.primary.connect('owner-change',
                                             self.owner_change)
            self.c_id = self.clipboard.connect('owner-change',
                                               self.owner_change)
            # Handle socket connections
            GObject.io_add_watch(self.sock_s, GObject.IO_IN,
                                 self.socket_listen)
            # Handle unix signals
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.exit)
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.exit)
            Gtk.main()


def main():
    """Start the application."""

    # Set a default config file
    clipster_dir = os.path.join(os.environ.get('HOME'), ".clipster")
    config_file = os.path.join(clipster_dir, "config")

    parser = argparse.ArgumentParser(description='Clipster clipboard manager.')
    parser.add_argument('-f', '--config', action="store",
                        default=config_file,
                        help="Path to config file.")
    parser.add_argument('-p', '--primary', action="store_true",
                        help="Write STDIN to the PRIMARY clipboard.")
    parser.add_argument('-c', '--clipboard', action="store_true",
                        help="Write STDIN to the CLIPBOARD clipboard.")
    parser.add_argument('-d', '--daemon', action="store_true",
                        help="Launch the daemon.")
    parser.add_argument('-s', '--select', action="store_true",
                        help="Launch the clipboard history selection window.")

    args = parser.parse_args()

    # Set some config defaults
    config_defaults = {"clipster_dir": clipster_dir,  # clipster 'root' dir
                       "default_selection": "PRIMARY",  # PRIMARY or CLIPBOARD
                       "history_file": "%(clipster_dir)s/history",
                       "socket_file": "%(clipster_dir)s/clipster_sock",
                       "pid_file":  "%(clipster_dir)s/clipster.pid",
                       "max_input": "50000",}  # max length of selection input

    config = configparser.SafeConfigParser(config_defaults)
    config.add_section('clipster')

    # If a config file arg is passed in, try reading it
    if args.config:
        config.read(args.config)
    else:
        # Try reading the config file from the defauilt dir
        config.read(config_file)

    # Override clipdir, if it's an option in the config file
    try:
        clipdir = config.get('clipster', 'clipster_dir')
    except configparser.Error:
        # Otherwise, set the value back into the config
        config.set('clipster', 'clipster_dir', clipdir)

    stdin = ""
    if not sys.stdin.isatty():
        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
            stdin = sys.stdin.read()

    clipster = Clipster(config, stdin)

    # Launch the daemon
    if args.daemon:
        clipster.Daemon(config).run()

    client_action = "BOARD"
    if args.select:
        client_action = "SELECT"

    if args.primary:
        config.set('clipster', 'default_selection', 'PRIMARY')
    elif args.clipboard:
        config.set('clipster', 'default_selection', 'CLIPBOARD')

    clipster.client(client_action)


if __name__ == "__main__":
    main()
