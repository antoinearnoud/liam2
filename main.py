from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import argparse
import os
from os.path import splitext
import platform
import warnings

import yaml

from liam2 import config
from liam2.simulation import Simulation
from liam2.importer import csv2h5
from liam2.console import Console
from liam2.utils import AutoFlushFile, QtAvailable, QtProgressBar, QtGui
from liam2 import registry
from liam2.data import populate_registry, H5Data
from liam2.upgrade import upgrade
from liam2.view import viewhdf

__version__ = "0.8.2"


def eat_traceback(func, *args, **kwargs):
    # e.context      | while parsing a block mapping
    # e.context_mark | in "import.yml", line 18, column 9
    # e.problem      | expected <block end>, but found '<block sequence start>'
    # e.problem_mark | in "import.yml", line 29, column 12
    error_log_path = None
    try:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            try:
                import traceback
                # output_directory might not be set at this point yet and it is
                # only set for run and explore commands but when it is not set
                # its default value of "." is used and thus we get the "old"
                # behaviour: error.log in the working directory
                out_dir = config.output_directory
                error_path = os.path.join(out_dir, 'error.log')
                error_path = os.path.abspath(error_path)
                with open(error_path, 'w') as f:
                    traceback.print_exc(file=f)
                error_log_path = error_path
            except IOError as log_ex:
                print(
                    "WARNING: %s on '%s'" % (log_ex.strerror, log_ex.filename))
            except Exception as log_ex:
                print(log_ex)
            raise e
    except yaml.parser.ParserError as e:
        # eg, inconsistent spacing, no space after a - in a list, ...
        print("SYNTAX ERROR %s" % str(e.problem_mark).strip())
    except yaml.scanner.ScannerError as e:
        # eg, tabs, missing colon for mapping. The reported problem is
        # different when it happens on the first line (no context_mark) and
        # when it happens on a subsequent line.
        if e.context_mark is not None:
            if e.problem == "could not found expected ':'":
                msg = "could not find expected ':'"
            else:
                msg = e.problem
            mark = e.context_mark
        else:
            if (
                e.problem == "found character '\\t' that cannot start any "
                             "token"):
                msg = "found a TAB character instead of spaces"
            else:
                msg = ""
            mark = e.problem_mark
        if msg:
            msg = ": " + msg
        print("SYNTAX ERROR %s%s" % (str(mark).strip(), msg))
    except yaml.reader.ReaderError as e:
        if e.encoding == 'utf8':
            print("\nERROR in '%s': invalid character found, this probably "
                  "means you have used non ASCII characters (accents and "
                  "other non-english characters) and did not save your file "
                  "using the UTF8 encoding" % e.name)
        else:
            raise
    except SyntaxError as e:
        print("SYNTAX ERROR:", e.msg.replace('EOF', 'end of block'))
        if e.text is not None:
            print(e.text)
            offset_str = ' ' * (e.offset - 1) if e.offset > 0 else ''
            print(offset_str + '^')
    except Exception as e:
        print("\nERROR:", str(e))

    if error_log_path is not None:
        print()
        print("the technical error log can be found at", error_log_path)


class GUI(object):
    def __init__(self):
        self.app = QtGui.QApplication(sys.argv)
        self.notifyProgress = None
        self.notifyEnd = None

    def simulate(self, simulation, run_console):
        pb = QtProgressBar(simulation.periods,
                           title="LIAM2: simulation progress",
                           valuelabel="period")
        self.notifyProgress = pb.dialog.notifyProgress
        self.notifyProgress.connect(pb.update)
        self.notifyEnd = pb.dialog.notifyEnd
        self.notifyEnd.connect(pb.destroy)

        simulation.run(run_console, self)


# This is what we should use because it makes the GUI responsive, but it needs
# far reaching changes: charts do not work anymore, we would probably need to
# have all "prints" & interactive input/console done by the UI thread,
# with messages to display sent by the "compute" thread (in signal arguments).
# for the charts, I do not really know how to proceed. Ideally the chart
# should be computed in the "compute thread" and displayed only when ready,
# but I don't know if a "Window" can be sent by a signal.
# see
# http://matplotlib.org/examples/user_interfaces/embedding_in_qt4.html
# class ThreadedGUI(object):
#     def __init__(self):
#         self.app = QtGui.QApplication(sys.argv)
#         self.notifyProgress = None
#         self.notifyEnd = None
#
#     def simulate(self, simulation, run_console):
#         pb = QtProgressBar(simulation.periods,
#                            title="LIAM2: simulation progress",
#                            valuelabel="period")
#         task = TaskThread(simulation.run, run_console, self)
#         task.notifyProgress.connect(pb.update)
#         self.notifyProgress = task.notifyProgress
#
#         # Stops the Qt event loop when the simulation thread signals it has
#         # finished. This effectively makes the app.exec_() call return (it was
#         # blocking so far)
#         task.notifyEnd.connect(self.app.quit)
#         self.notifyEnd = task.notifyEnd
#
#         task.start()
#
#         # enter the Qt event loop
#         self.app.exec_()


def simulate(args):
    print("Using simulation file: '%s'" % args.file)

    simulation = Simulation.from_yaml(args.file, input_dir=args.input_path,
                                      input_file=args.input_file,
                                      output_dir=args.output_path,
                                      output_file=args.output_file)
    if args.progress:
        if not QtAvailable:
            raise Exception("Qt is not available, cannot use -p/--progress")
        gui = GUI()
        gui.simulate(simulation, args.interactive)
    else:
        simulation.run(args.interactive)


#    import cProfile as profile
#    profile.runctx('simulation.run(args.interactive)', vars(), {},
#                   'c:\\tmp\\simulation.profile')
# to use profiling data:
# import pstats
# p = pstats.Stats('c:\\tmp\\simulation.profile')
# p.strip_dirs().sort_stats('cum').print_stats(30)


def explore(fpath):
    _, ext = splitext(fpath)
    ftype = 'data' if ext in ('.h5', '.hdf5') else 'simulation'
    print("Using %s file: '%s'" % (ftype, fpath))
    if ftype == 'data':
        globals_def = populate_registry(fpath)
        data_source = H5Data(None, fpath)
        h5in, _, globals_data = data_source.load(globals_def,
                                                 registry.entity_registry)
        h5out = None
        entity, period = None, None
    else:
        simulation = Simulation.from_yaml(fpath)
        h5in, h5out, globals_data = simulation.load()
        entity = simulation.console_entity
        period = simulation.start_period + simulation.periods - 1
        globals_def = simulation.globals_def
    try:
        c = Console(entity, period, globals_def, globals_data)
        c.run()
    finally:
        h5in.close()
        if h5out is not None:
            h5out.close()


def display(fpath):
    print("Launching ViTables...")
    _, ext = splitext(fpath)
    if ext in ('.h5', '.hdf5'):
        files = [fpath]
    else:
        ds = Simulation.from_yaml(fpath).data_source
        files = [ds.input_path, ds.output_path]
    print("Trying to open:", " and ".join(str(f) for f in files))
    viewhdf(files)


class PrintVersionsAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        import numpy
        import numexpr
        import bcolz
        import tables

        try:
            from cpartition import filter_to_indices

            del filter_to_indices
            cext = True
        except ImportError:
            cext = False
        print("C extensions are" + (" NOT" if not cext else "") + " available")

        py_version = '{} ({})'.format(platform.python_version(),
                                      platform.architecture()[0])
        print("""
python {py}
numpy {np}
numexpr {ne}
pytables {pt}
bcolz {ca}
pyyaml {yml}""".format(py=py_version, np=numpy.__version__,
                       ne=numexpr.__version__, pt=tables.__version__,
                       ca=bcolz.__version__, yml=yaml.__version__))
        parser.exit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--versions', action=PrintVersionsAction, nargs=0,
                        help="display versions of dependencies")
    parser.add_argument('--debug', action='store_true', default=False,
                        help="run in debug mode")
    parser.add_argument('--input-path', dest='input_path',
                        help='override the input path')
    parser.add_argument('--input-file', dest='input_file',
                        help='override the input file')
    parser.add_argument('--output-path', dest='output_path',
                        help='override the output path')
    parser.add_argument('--output-file', dest='output_file',
                        help='override the output file')

    subparsers = parser.add_subparsers(dest='action')

    # create the parser for the "run" command
    parser_run = subparsers.add_parser('run', help='run a simulation')
    parser_run.add_argument('file', help='simulation file')
    parser_run.add_argument('-i', '--interactive', action='store_true',
                            help='show the interactive console after the '
                                 'simulation')
    parser_run.add_argument('-p', '--progress', action='store_true',
                            help='show the simulation progress in a window')

    # create the parser for the "import" command
    parser_import = subparsers.add_parser('import', help='import data')
    parser_import.add_argument('file', help='import file')

    # create the parser for the "explore" command
    parser_explore = subparsers.add_parser('explore', help='explore data of a '
                                                           'past simulation')
    parser_explore.add_argument('file', help='explore file')

    # create the parser for the "upgrade" command
    parser_upgrade = subparsers.add_parser('upgrade',
                                           help='upgrade a simulation file to '
                                                'the latest syntax')
    parser_upgrade.add_argument('input', help='input simulation file')
    out_help = "output simulation file. If missing, the original file will " \
               "be backed up (to filename.bak) and the upgrade will be " \
               "done in-place."
    parser_upgrade.add_argument('output', help=out_help, nargs='?')

    # create the parser for the "view" command
    parser_import = subparsers.add_parser('view', help='view data')
    parser_import.add_argument('file', help='data file')

    parsed_args = parser.parse_args()
    if parsed_args.debug:
        config.debug = True

    # this can happen via the environment variable too!
    if config.debug:
        warnings.simplefilter('default')
        wrapper = lambda func, *args, **kwargs: func(*args, **kwargs)
    else:
        wrapper = eat_traceback

    action = parsed_args.action
    if action == 'run':
        args = simulate, parsed_args
    elif action == "import":
        args = csv2h5, parsed_args.file
    elif action == "explore":
        args = explore, parsed_args.file
    elif action == "upgrade":
        args = upgrade, parsed_args.input, parsed_args.output
    elif action == "view":
        args = display, parsed_args.file
    else:
        raise Exception("invalid action: %s" % action)
    wrapper(*args)


if __name__ == '__main__':
    import sys

    sys.stdout = AutoFlushFile(sys.stdout)
    sys.stderr = AutoFlushFile(sys.stderr)

    print("LIAM2 %s (%s)" % (__version__, platform.architecture()[0]))
    print()

    main()