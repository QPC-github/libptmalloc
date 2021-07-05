from __future__ import print_function

import argparse
import binascii
import struct
import sys
import logging
import importlib
import pprint
import re
import pickle

import libptmalloc.frontend.printutils as pu
importlib.reload(pu)
import libptmalloc.frontend.helpers as h
importlib.reload(h)
import libptmalloc.frontend.commands.gdb.ptcmd as ptcmd # no reload on purpose

log = logging.getLogger("libptmalloc")
log.trace("ptmeta.py")

try:
    import gdb
except ImportError:
    print("Not running inside of GDB, exiting...")
    raise Exception("sys.exit()")  

meta_cache = {}
backtrace_ignore = set([])

colorize_table = {
    "red": pu.red,
    "green": pu.green,
    "yellow": pu.yellow,
    "blue": pu.blue,
    "purple": pu.purple,
    "cyan": pu.cyan,
    "gray": pu.gray,
    "lightred": pu.light_red,
    "lightgreen": pu.light_green,
    "lightyellow": pu.light_yellow,
    "lightblue": pu.light_blue,
    "lightpurple": pu.light_purple,
    "lightcyan": pu.light_cyan,
    "lightgray": pu.light_gray,
    "white": pu.white,
    "black": pu.black,
}

METADATA_DB = "metadata.pickle"
def save_metadata_to_file(filename):
    """During development, we reload libptmalloc and lose the metadata database
    so this allows saving it easily into a file before doing so
    """
    d = {}
    d["meta_cache"] = meta_cache
    d["backtrace_ignore"] = backtrace_ignore
    pickle.dump(d, open(filename, "wb"))

def load_metadata_from_file(filename):
    """During development, we reload libptmalloc and lose the metadata database
    so this allows reloading it easily from a file
    """
    global meta_cache, backtrace_ignore
    d = pickle.load(open(filename, "rb"))
    meta_cache = d["meta_cache"]
    backtrace_ignore = d["backtrace_ignore"]

def get_metadata(address, list_metadata=[]):
    """
    :param address: the address to retrieve metatada from
    :param list_metadata: If a list, the list of metadata to retrieve (even empty list).
                          If the "all" string, means to retrieve all metadata
    :return: the following L, suffix, epilog, colorize_func
    """

    L = [] # used for json output
    suffix = "" # used for one-line output
    epilog = "" # used for verbose output
    colorize_func = str # do not colorize by default

    if address not in meta_cache:
        epilog += "chunk address not found in metadata database\n"
        return None, suffix, epilog, colorize_func

    # This allows calling get_metadata() by not specifying any metadata
    # but meaning we want to retrieve them all
    if list_metadata == "all":
        list_metadata = list(meta_cache[address].keys())
        if "backtrace" in list_metadata:
            # enforce retrieving all the functions from the backtrace
            list_metadata.remove("backtrace")
            list_metadata.append("backtrace:-1")

    opened = False
    for key in list_metadata:
        param = None
        if ":" in key:
            key, param = key.split(":")
        if key not in meta_cache[address]:
            if key != "color":
                suffix += " | N/A"
                epilog += "'%s' key not found in metadata database\n" % key
                opened = True
                L.append(None)
            continue
        if key == "backtrace":
            if param == None:
                funcs_list = get_first_function(address)
            else:
                funcs_list = get_functions(address, max_len=int(param))
            if funcs_list == None:
                suffix += " | N/A"
            elif len(funcs_list) == 0:
                # XXX - atm if we failed to parse the functions from the debugger
                # we will also show "filtered" even if it is not the case
                suffix += " | filtered"
            else:
                suffix += " | %s" % ",".join(funcs_list)
            epilog += "%s" % meta_cache[address]["backtrace"]["raw"]
            L.append(funcs_list)
            opened = True
        elif key == "color":
            color = meta_cache[address][key]
            colorize_func = colorize_table[color]
        else:
            suffix += " | %s" % meta_cache[address][key]
            epilog += "%s\n" % meta_cache[address][key]
            L.append(meta_cache[address][key])
            opened = True
    if opened:
        suffix += " |"

    return L, suffix, epilog, colorize_func

def get_first_function(address):
    return get_functions(address, max_len=1)

def get_functions(address, max_len=None):
    L = []
    if address not in meta_cache:
        return None
    if "backtrace" not in meta_cache[address]:
        return None
    funcs = meta_cache[address]["backtrace"]["funcs"]
    for f in funcs:
        if f in backtrace_ignore:
            continue
        L.append(f)
        if max_len != None and len(L) == max_len:
            break
    return L

class ptmeta(ptcmd.ptcmd):
    """Command to manage metadata for a given address"""

    def __init__(self, ptm):
        log.debug("ptmeta.__init__()")
        super(ptmeta, self).__init__(ptm, "ptmeta")

        self.parser = argparse.ArgumentParser(
            description="""Handle metadata associated with chunk addresses""", 
            formatter_class=argparse.RawTextHelpFormatter,
            add_help=False,
            epilog="""NOTE: use 'ptmeta <action> -h' to get more usage info""")
        self.parser.add_argument(
            "-v", "--verbose", dest="verbose", action="count", default=0,
            help="Use verbose output (multiple for more verbosity)"
        )
        self.parser.add_argument(
            "-h", "--help", dest="help", action="store_true", default=False,
            help="Show this help"
        )

        actions = self.parser.add_subparsers(
            help="Action to perform", 
            dest="action"
        )

        add_parser = actions.add_parser(
            "add",
            help="""Save metadata for a given chunk address""",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog="""The saved metadata can then be shown in any other commands like 
'ptlist', 'ptchunk', 'pfree', etc.

E.g.
  ptmeta add mem-0x10 tag "service_user struct"
  ptmeta add 0xdead0030 color green
  ptmeta add 0xdead0030 backtrace"""
        )
        add_parser.add_argument(
            'address', 
            help='Address to link the metadata to'
        )
        add_parser.add_argument(
            'key', 
            help='Key name of the metadata (e.g. "backtrace", "color", "tag" or any name)'
        )
        add_parser.add_argument(
            'value', nargs="?",
            help='Value of the metadata, associated with the key (required except when adding a "backtrace")'
        )
        
        del_parser = actions.add_parser(
            "del", 
            help="""Delete metadata associated with a given chunk address""",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog="""E.g.
  ptmeta del mem-0x10
  ptmeta del 0xdead0030"""
        )
        del_parser.add_argument('address', help='Address to remove the metadata for')
        
        list_parser = actions.add_parser(
            "list", 
            help="""List metadata for a chunk address or all chunk addresses (debugging)""",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog="""E.g.
  ptmeta list mem-0x10
  ptmeta list 0xdead0030 -M backtrace
  ptmeta list
  ptmeta list -vvvv
  ptmeta list -M "tag, backtrace:3"""
        )
        list_parser.add_argument(
            'address', nargs="?", 
            help='Address to remove the metadata for'
        )
        list_parser.add_argument(
            "-M", "--metadata", dest="metadata", type=str, default=None,
            help="Comma separated list of metadata to print"
        )

        config_parser = actions.add_parser(
            "config", 
            help="Configure general metadata behaviour",
            formatter_class=argparse.RawTextHelpFormatter,
            epilog="""E.g.
  ptmeta config ignore backtrace _nl_make_l10nflist __GI___libc_free"""
        )
        config_parser.add_argument(
            'feature',  
            help='Feature to configure (e.g. "ignore")'
        )
        config_parser.add_argument(
            'key', 
            help='Key name of the metadata (e.g. "backtrace")'
        )
        config_parser.add_argument(
            'values', nargs="+",
            help='Values of the metadata, associated with the key (e.g. list of function to ignore in a backtrace)'
        )

        # allows to enable a different log level during development/debugging
        self.parser.add_argument(
            "--loglevel", dest="loglevel", default=None,
            help=argparse.SUPPRESS
        )
        # allows to save metadata to file during development/debugging
        self.parser.add_argument(
            "-S", "--save-db", dest="save", action="store_true", default=False,
            help=argparse.SUPPRESS
        )
        # allows to load metadata from file during development/debugging
        self.parser.add_argument(
            "-L", "--load-db", dest="load", action="store_true", default=False,
            help=argparse.SUPPRESS
        )

    @h.catch_exceptions
    @ptcmd.ptcmd.init_and_cleanup
    def invoke(self, arg, from_tty):
        """Inherited from gdb.Command
        See https://sourceware.org/gdb/current/onlinedocs/gdb/Commands-In-Python.html
        """

        log.debug("ptmeta.invoke()")

        if self.args.action is None and not self.args.save and not self.args.load:
            pu.print_error("WARNING: requires an action")
            self.parser.print_help()
            return

        if self.args.action == "list" \
        or self.args.action == "add" \
        or self.args.action == "del":
            address = None
            if self.args.address != None:
                addresses = self.dbg.parse_address(self.args.address)
                if len(addresses) == 0:
                    pu.print_error("WARNING: No valid address supplied")
                    self.parser.print_help()
                    return
                address = addresses[0]
 
        if self.args.action == "list":
            self.list_metadata(address)
            return

        if self.args.action == "del":
            self.delete_metadata(address)
            return

        if self.args.action == "config":
            self.configure_metadata(self.args.feature, self.args.key, self.args.values)
            return

        if self.args.action == "add":
            self.add_metadata(address, self.args.key, self.args.value)
            return

        if self.args.save:
            if self.args.verbose >= 0: # always print since debugging feature
                print("Saving metadata database to file...")
            save_metadata_to_file(METADATA_DB)
            return

        if self.args.load:
            if self.args.verbose >= 0: # always print since debugging feature
                print("Loading metadata database from file...")
            load_metadata_from_file(METADATA_DB)
            return

    def list_metadata(self, address):
        """Show the metadata database for all addresses or a given address

        if verbose == 0, shows single-line entries (no "backtrace" if not requested)
        if verbose == 1, shows single-line entries (all keys)
        if verbose == 2, shows multi-line entries (no "backtrace" if not requested)
        if verbose == 3, shows multi-line entries (all keys)
        """

        if len(meta_cache) != 0:
            pu.print_header("Metadata database", end=None)

            if self.args.metadata == None:
                # if no metadata provided by user, we get them all
                list_metadata = []
                for k, d in meta_cache.items():
                    for k2, d2 in d.items():
                        if k2 not in list_metadata:
                            list_metadata.append(k2)
                if self.args.verbose == 0 and "backtrace" in list_metadata:
                    list_metadata.remove("backtrace")
            else:
                list_metadata = [e.strip() for e in self.args.metadata.split(",")]

            if self.args.verbose <= 1:
                print("| address | ", end="")
                print(" | ".join(list_metadata), end="")
                print(" |")
                for k, d in meta_cache.items():
                    if address == None or k == address:
                        L, s, e, colorize_func = get_metadata(k, list_metadata=list_metadata)
                        addr = colorize_func(f"0x{k:x}")
                        print(f"| {addr}", end="")
                        print(s)
            else:
                for k, d in meta_cache.items():
                    if address == None or k == address:
                        L, s, e, colorize_func = get_metadata(k, list_metadata=list_metadata)
                        addr = colorize_func(f"0x{k:x}")
                        print(f"{addr}:")
                        print(e)
        else:
            pu.print_header("Metadata database", end=None)
            print("N/A")
        
        print("")
        
        if len(backtrace_ignore) != 0:
            pu.print_header("Function ignore list for backtraces", end=None)
            pprint.pprint(backtrace_ignore)
        else:
            pu.print_header("Function ignore list for backtraces", end=None)
            print("N/A")

    def configure_metadata(self, feature, key, values):
        """Save given metadata (key, values) for a given feature (e.g. "backtrace")

        :param feature: name of the feature (e.g. "ignore")
        :param key: name of the metadata (e.g. "backtrace")
        :param values: list of values to associate to the key
        """

        if self.args.verbose >= 1:
            print("Configuring metadata database...")
        if key == "backtrace":
            if feature == "ignore":
                backtrace_ignore.update(values)
            else:
                pu.print_error("WARNING: Unsupported feature")
                return
        else:
            pu.print_error("WARNING: Unsupported key")
            return

    def delete_metadata(self, address):
        """Delete metadata for a given chunk's address
        """

        if address not in meta_cache:
            return

        if self.args.verbose >= 1:
            print(f"Deleting metadata for {address} from database...")
        del meta_cache[address]

    def add_metadata(self, address, key, value):
        """Save given metadata (key, value) for a given chunk's address
        E.g. key = "tag" and value is an associated user-defined tag
        """

        if self.args.verbose >= 1:
            print("Adding to metadata database...")
        if key == "backtrace":
            result = self.dbg.get_backtrace()
        elif key == "color":
            if value not in colorize_table:
                pu.print_error(f"ERROR: Unsupported color. Need one of: {', '.join(colorize_table.keys())}")
                return
            result = value
        else:
            result = value

        if address not in meta_cache:
            meta_cache[address] = {}    
        meta_cache[address][key] = result

