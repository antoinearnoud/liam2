from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
#import bcolz
import numpy as np
import tables

from . import config
from .context import EntityContext, context_length
from .data import merge_arrays, get_fields, ColumnArray
from .expr import (Variable, VariableMethodHybrid, GlobalVariable, GlobalTable,
                   GlobalArray, expr_eval, get_missing_value, Expr,
                   MethodSymbol)
from .exprparser import parse
from .process import Assignment, Compute, Process, ProcessGroup, While, Function
from .registry import entity_registry
from .utils import (safe_put, count_occurrences, field_str_to_type, size2str,
                    WarnOverrideDict)


max_vars = 0


#def compress_column(a, level):
#    arr = bcolz.carray(a, cparams=bcolz.cparams(level))
#    print "%d -> %d (%.2f)" % (arr.nbytes, arr.cbytes,
#                               float(arr.nbytes) / arr.cbytes),
#    return arr
#
#
#def decompress_column(a):
#    return a[:]

# TODO: rename to global_symbols
def global_variables(globals_def):
    #FIXME: these should be computed once somewhere else, not for each
    # entity. I guess they should have a class of their own
    # TODO: rename to symbols
    variables = {}
    for name, global_def in globals_def.items():
        global_type = global_def.get('fields')
        if isinstance(global_type, list):
            # add namespace for table
            variables[name] = GlobalTable(name, global_type)
            if name == 'periodic':
                # special case to add periodic variables in the global
                # namespace
                variables.update(
                    (name, GlobalVariable('periodic', name, type_))
                    for name, type_ in global_type)
        else:
            global_type = global_def['type']
            assert isinstance(global_type, type), "not a type: %s" % global_type
            variables[name] = GlobalArray(name, global_type)
    return variables


# This is an awful workaround for the fact that tables.Array does not support
# fancy indexes with negative indices.
# See https://github.com/PyTables/PyTables/issues/360
class DiskBackedArray(object):
    def __init__(self, arr):
        self.arr = arr

    def __getitem__(self, item):
        # load the array entirely in memory before indexing it
        return self.arr[:][item]

    def __getattr__(self, item):
        return getattr(self.arr, item)


class Entity(object):
    """
    fields is a list of tuple (name, type)
    """
    def __init__(self, name, fields=None, missing_fields=None, links=None,
                 macro_strings=None, process_strings=None,
                 array=None):
        self.name = name

        # we should have exactly one of either array or fields defined
        assert ((fields is None and array is not None) or
                (fields is not None and array is None))

        if array is not None:
            if fields is None:
                fields = get_fields(array)
            array_period = np.min(array['period'])
        else:
            array_period = None

        duplicate_names = [name
                           for name, num
                           in count_occurrences(fname for fname, _ in fields)
                           if num > 1]
        if duplicate_names:
            raise Exception("duplicate fields in entity '%s': %s"
                            % (self.name, ', '.join(duplicate_names)))
        fnames = [name for name, _ in fields]
        if 'id' not in fnames:
            fields.insert(0, ('id', int))
        if 'period' not in fnames:
            fields.insert(0, ('period', int))
        self.fields = fields

        # only used in data (to check that all "required" fields are present
        # in the input file)

        # one potential solution would be to split the fields argument and
        # attribute in input_fields and output_fields (regardless of whether
        # it is split in the simulation/yaml file).

        # however that might be just a temporary solution as we will soon need
        # more arguments to fields (default values, ranges, etc...)

        # another solution is to use a Field class
        # seems like the better long term solution
        self.missing_fields = missing_fields
        self.stored_fields = set(name for name, _ in fields)
        self.links = links

        self.macro_strings = macro_strings
        self.process_strings = process_strings
        self.processes = None

        self.expectedrows = tables.parameters.EXPECTED_ROWS_TABLE
        self.table = None
        self.input_table = None

        self.indexed_input_table = None
        self.indexed_output_table = None

        self.input_rows = {}
        #TODO: it is unnecessary to keep periods which have already been
        # simulated, because (currently) when we go back in time, we always go
        # back using the output table... but periods before the start_period
        # are only present in input_index
        #FIXME: the proper way to fix this is to copy the input_index into
        # the output_index during H5Date.run() and not keep input_index
        # beyond that point.
        self.input_index = {}

        self.output_rows = {}
        self.output_index = {}
        self.output_index_node = None

        self.base_period = None
        # we need a separate field, instead of using array['period'] to be able
        # to get the period even when the array is empty.
        self.array_period = array_period
        self.array = None

        self.lag_fields = []
        self.array_lag = None

        self.num_tmp = 0
        self.temp_variables = {}
        self.id_to_rownum = None
        self._variables = None
        self._methods = None

    @classmethod
    def from_yaml(cls, ent_name, entity_def):
        from .links import Many2One, One2Many

        # YAML "ordered dict" syntax returns a list of dict and we want a list
        # of tuples
        #FIXME: if "fields" key is present but no field is defined,
        #entity_def.get('fields', []) returns None and this breaks
        fields_def = [list(d.items())[0] for d in entity_def.get('fields', [])]

        fields = []
        missing_fields = []
        for name, fielddef in fields_def:
            if isinstance(fielddef, dict):
                strtype = fielddef['type']
                if not fielddef.get('initialdata', True):
                    missing_fields.append(name)
            else:
                strtype = fielddef
            fields.append((name,
                           field_str_to_type(strtype, "field '%s'" % name)))

        link_defs = entity_def.get('links', {})
        str2class = {'one2many': One2Many, 'many2one': Many2One}
        links = dict((name,
                      str2class[l['type']](name, l['field'], l['target']))
                     for name, l in link_defs.items())

        return Entity(ent_name, fields, missing_fields, links,
                      entity_def.get('macros', {}),
                      entity_def.get('processes', {}))

    @property
    def local_var_names(self):
        return set(self.temp_variables.keys()) - set(self.variables.keys())

    @classmethod
    def from_table(cls, table):
        return Entity(table.name, get_fields(table), missing_fields=[],
                      links={}, macro_strings={}, process_strings={})

    @staticmethod
    def collect_predictors(items):
        # this excludes lists (procedures) and dict (while, ...)
        return [k for k, v in items
                if k is not None and isinstance(v, (str, int, float))]

    @property
    def variables(self):
        if self._variables is None:
            processes = list(self.process_strings.items())

            # names of all processes (hybrid or not) of the entity
            process_names = set(k for k, v in processes if k is not None)

            # names of all entity variables (temporary or not) which are set
            # globally
            all_predictors = set(self.collect_predictors(processes))

            stored_fields = self.stored_fields

            # non-callable fields (no variable-procedure for them)
            variables = dict((name, Variable(name, type_))
                             for name, type_ in self.fields
                             if name in stored_fields - process_names)
            # callable fields
            variables.update((name, VariableMethodHybrid(name, self, type_)) for
                             name, type_ in self.fields if
                             name in stored_fields & process_names)
            # global temporaries (they are all callable)
            variables.update((name, VariableMethodHybrid(name, self))
                             for name in all_predictors - stored_fields)
            variables.update(self.links)
            self._variables = variables
        return self._variables

    @staticmethod
    def ismethod(v):
        keys = ('args', 'code', 'return')
        return (isinstance(v, list) or
                isinstance(v, dict) and any(key in v for key in keys))

    @property
    def methods(self):
        if self._methods is None:
            # variable-method hybrids are handled by the self.variable property
            self._methods = [(key, MethodSymbol(key, self))
                             for key, value in self.process_strings.items()
                             if self.ismethod(value) and
                                key not in self.stored_fields]
        return self._methods

    def check_links(self):
        for name, link in self.links.items():
            #noinspection PyProtectedMember
            target_name = link._target_entity_name
            if target_name not in entity_registry:
                raise Exception("Target of '%s' link in entity '%s' is an "
                                "unknown entity (%s)" % (name, self.name,
                                                         target_name))

    def get_cond_context(self, entities_visited=None):
        """returns the conditional context: {link: variables}"""

        if entities_visited is None:
            entities_visited = set()
        else:
            entities_visited = entities_visited.copy()
        entities_visited.add(self)

        linked_entities = {}
        for k, link in list(self.links.items()):
            #noinspection PyProtectedMember
            entity = link._target_entity()
            if entity not in entities_visited:
                linked_entities[k] = entity

        cond_context = {}
        # use a set of entities to compute the conditional context only once
        # per target entity
        for entity in set(linked_entities.values()):
            cond_context.update(entity.get_cond_context(entities_visited))

        # entities linked directly take priority over (override) farther ones
        cond_context.update((k, entity.variables)
                            for k, entity in list(linked_entities.items()))
        return cond_context
    conditional_context = property(get_cond_context)

    def all_symbols(self, globals_def):
        from .links import PrefixingLink

        symbols = global_variables(globals_def).copy()
        symbols = WarnOverrideDict(symbols)
        symbols.update(self.variables)
        cond_context = self.conditional_context
        macros = dict((k, parse(v, symbols, cond_context))
                      for k, v in self.macro_strings.items())
        symbols.update(macros)
        symbols['other'] = PrefixingLink(macros, self.links, '__other_')
        symbols.update(self.methods)
        return symbols

    def parse_expr(self, k, v, variables, cond_context):
        if isinstance(v, (bool, int, float)):
            return Assignment(v)
        elif isinstance(v, str):
            expr = parse(v, variables, cond_context)
            if isinstance(expr, Process):
                return expr
            else:
                if k is None:
                    return Compute(expr)
                else:
                    return Assignment(expr)
        else:
            # lets be explicit about it
            return None

    def parse_process_group(self, k, v, variables, cond_context, purge=True):
        # v is a procedure
        # it should be a list of dict (assignment) or string (action)
        group_expressions = [list(elem.items())[0] if isinstance(elem, dict)
                             else (None, elem)
                             for elem in v]
        group_predictors = \
            self.collect_predictors(group_expressions)
        group_context = variables.copy()
        group_context.update((name, Variable(name))
                             for name in group_predictors)
        sub_processes = self.parse_expressions(group_expressions,
                                               group_context,
                                               cond_context)
        return ProcessGroup(k, sub_processes, purge)

    def parse_expressions(self, items, context, cond_context):
        """
        items -- a list of tuples (name, process_string)
        context -- a dict of all symbols available in the scope
        cond_context --
        """
        processes = []
        for k, v in items:
            if k == 'while':
                if not isinstance(v, dict):
                    raise ValueError("while is a reserved keyword")
                cond = parse(v['cond'], context, cond_context)
                assert isinstance(cond, Expr)
                code = self.parse_process_group("while:code", v['code'],
                                                context, cond_context,
                                                purge=False)
                process = While(cond, code)
            else:
                process = self.parse_expr(k, v, context, cond_context)
                if process is None:
                    if self.ismethod(v):
                        if isinstance(v, list):
                            # v should be a list of dicts (assignments) or
                            # strings (actions)
                            argnames, code_def, result_def = [], v, None
                        else:
                            assert isinstance(v, dict)
                            args_def = v.get('args', '')
                            argnames = [a.strip()
                                        for a in args_def.split(',')
                                        if a != '']
                            code_def = v.get('code', [])
                            result_def = v.get('return')
                        method_context = context.copy()
                        method_context.update((name, Variable(name))
                                              for name in argnames)
                        code = self.parse_process_group("func:code", code_def,
                                                        method_context,
                                                        cond_context,
                                                        purge=False)
                        #TODO: use code.predictors instead (but it currently
                        # fails for some reason)
                        group_expressions = [list(elem.items())[0] if isinstance(elem, dict)
                                             else (None, elem)
                                             for elem in code_def]
                        group_predictors = \
                            self.collect_predictors(group_expressions)
                        method_context.update((name, Variable(name))
                                              for name in group_predictors)
                        result = parse(result_def, method_context,
                                       cond_context)
                        assert result is None or isinstance(result, Expr)
                        process = Function(argnames, code, result)
                    elif isinstance(v, dict) and 'predictor' in v:
                        raise ValueError("Using the 'predictor' keyword is "
                                         "not supported anymore. "
                                         "If you need several processes to "
                                         "write to the same variable, you "
                                         "should rather use procedures.")
                    else:
                        raise Exception("unknown expression type for %s: %s"
                                        % (k, type(v)))
            processes.append((k, process))
        return processes

    def parse_processes(self, globals_def):
        processes = self.parse_expressions(iter(self.process_strings.items()),
                                           self.all_symbols(globals_def),
                                           self.conditional_context)
        # attach processes
        # TODO: make actions inherit from Expr instead of Process, and wrap
        # them in a Compute process so that I can kill attach
        for k, v in processes:
            v.attach(k, self)

        self.processes = dict(processes)

    def compute_lagged_fields(self):
        from .tfunc import Lag
        from .links import LinkValue
        lag_vars = set()
        for p in self.processes.values():
            for expr in p.expressions():
                for node in expr.all_of(Lag):
                    for v in node.all_of(Variable):
                        if not isinstance(v, GlobalVariable):
                            lag_vars.add(v.name)
                    for lv in node.all_of(LinkValue):
                        #noinspection PyProtectedMember
                        lag_vars.add(lv.link._link_field)
                        #noinspection PyProtectedMember
                        target_entity = lv.link._target_entity()
                        if target_entity == self:
                            target_vars = lv.target_expression.all_of(Variable)
                            lag_vars.update(v.name for v in target_vars)

        if lag_vars:
            # make sure we have an 'id' column, and that it comes first
            # (makes debugging easier). 'id' is always necessary for lag
            # expressions to be able to "expand" the vector of values to the
            # "current" individuals.
            lag_vars.discard('id')
            lag_vars = ['id'] + sorted(lag_vars)

        field_type = dict(self.fields)
        self.lag_fields = [(v, field_type[v]) for v in lag_vars]

    def load_period_data(self, period):
        if self.lag_fields:
            #TODO: use ColumnArray here
            #XXX: do we need np.empty? (but watch for alias problems)
            self.array_lag = np.empty(len(self.array),
                                      dtype=np.dtype(self.lag_fields))
            for field, _ in self.lag_fields:
                self.array_lag[field] = self.array[field]

        rows = self.input_rows.get(period)
        if rows is None:
            # nothing needs to be done in that case
            return

        start, stop = rows

        # It would be nice to use ColumnArray.from_table and adapt merge_arrays
        # to produce a ColumnArray in all cases, but it is not a huge priority
        # for now
        input_array = self.input_table.read(start, stop)

        self.array, self.id_to_rownum = \
            merge_arrays(self.array, input_array, result_fields='array1')
        # this can happen, depending on the layout of columns in input_array,
        # but the usual case (in retro) is that self.array is a superset of
        # input_array, in which case merge_arrays returns a ColumnArray
        if not isinstance(self.array, ColumnArray):
            self.array = ColumnArray(self.array)

    def purge_locals(self):
        """purge all local variables"""
        global max_vars

        temp_vars = self.temp_variables
        local_var_names = self.local_var_names
        num_locals = len(local_var_names)
        if config.debug and num_locals:
            local_vars = [v for k, v in temp_vars.items()
                          if k in local_var_names and isinstance(v, np.ndarray)]
            max_vars = max(max_vars, num_locals)
            temp_mem = sum(v.nbytes for v in local_vars)
            avgsize = sum(v.dtype.itemsize for v in local_vars) / num_locals
            print(("purging {} variables (max {}), will free {} of memory "
                   "(avg field size: {} b)".format(num_locals, max_vars,
                                                   size2str(temp_mem),
                                                   avgsize)))
        for var in local_var_names:
            del temp_vars[var]

    def flush_index(self, period):
        # keep an in-memory copy of the index for the current period
        self.output_index[period] = self.id_to_rownum

        # also flush it to disk
        h5file = self.output_index_node._v_file
        h5file.createArray(self.output_index_node, "_%d" % period,
                           self.id_to_rownum, "Period %d index" % period)

        # if an old index exists (this is not the case for the first period!),
        # point to the one on the disk, instead of the one in memory,
        # effectively clearing the one in memory
        idxname = '_%d' % (period - 1)
        if idxname in self.output_index_node:
            prev_disk_array = getattr(self.output_index_node, idxname)
            # DiskBackedArray is a workaround for pytables#360 (see above)
            self.output_index[period - 1] = DiskBackedArray(prev_disk_array)

    def store_period_data(self, period):
        if config.debug:
            temp_mem = sum(v.nbytes for v in self.temp_variables.values()
                           if isinstance(v, np.ndarray))
            main_mem = self.array.nbytes
            print("mem used: %s (main: %s / temp: %s)"
                  % (size2str(temp_mem + main_mem),
                     size2str(main_mem),
                     size2str(temp_mem)))

        # erase all temporary variables which have been computed this period
        self.temp_variables = {}

        if period in self.output_rows:
            raise Exception("trying to modify already simulated rows")

        startrow = self.table.nrows
        self.array.append_to_table(self.table)
        self.output_rows[period] = (startrow, self.table.nrows)
        self.flush_index(period)
        self.table.flush()

#    def compress_period_data(self, level):
#        compressed = bcolz.ctable(self.array, cparams=bcolz.cparams(level))
#        print "%d -> %d (%f)" % compressed._get_stats()

    @staticmethod
    def fill_missing_values(ids, values, context, filler='auto'):
        """
        ids: ids present in past period
        context: current period context
        """

        if filler is 'auto':
            filler = get_missing_value(values)
        result = np.empty(context_length(context), dtype=values.dtype)
        result.fill(filler)
        if len(ids):
            id_to_rownum = context.id_to_rownum
            # if there was more objects in the past than in the current
            # period. Currently, remove() keeps old ids, so this never
            # happens, but if we ever change remove(), we'll need to add
            # such a check everywhere we use id_to_rownum
#            invalid_ids = ids > len(id_to_rownum)
#            if np.any(invalid_ids):
#                fix ids
            rows = id_to_rownum[ids]
            safe_put(result, rows, values)
        return result

    def value_for_period(self, expr, period, context, fill='auto'):
        sub_context = EntityContext(self,
                                    {'period': period,
                                     '__globals__': context['__globals__']})
        result = expr_eval(expr, sub_context)
        if isinstance(result, np.ndarray) and result.shape:
            ids = expr_eval(Variable('id'), sub_context)
            if fill is None:
                return ids, result
            else:
                # expand values to the current "outer" context
                return self.fill_missing_values(ids, result, context, fill)
        else:
            return result

    def __repr__(self):
        return "<Entity '%s'>" % self.name
