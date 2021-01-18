"""
mztab - mzTab file reader
============================

Summary
-------

`mzTab <https://github.com/HUPO-PSI/mzTab>`_  is one of the standards
developed by the Proteomics Informatics working group of the HUPO Proteomics
Standard Initiative.

This module provides a way to read mzTab files into a collection of
:py:class:`pandas.DataFrame` instances in memory, along with a mapping
of the file-level metadata. MzTab specifications 1.0 and 2.0 are supported.

Data access
-----------

  :py:class:`MzTab` - a class representing a single mzTab file

"""

import re
try:
    import pandas as pd
except ImportError:
    pd = None


from collections import OrderedDict

from pyteomics.auxiliary import _file_obj
from pyteomics.auxiliary import cvstr
from pyteomics.auxiliary.utils import add_metaclass


def _require_pandas():
    if pd is None:
        raise ImportError(
            "To load an mzTab file into pandas.DataFrame objects, you must install pandas!")


class MetadataBackedProperty(object):
    '''Our descriptor type which uses the instance's metadata attribute to carry its values'''

    def __init__(self, name, variant_required=None):
        self.name = name
        self.variant_required = variant_required
        self.__doc__ = self.build_docstring()

    def __repr__(self):
        return "{self.__class__.__name__}(name={self.name!r}, variant_required={self.variant_required})".format(self=self)

    def __get__(self, obj, objtype=None):
        if obj is None and objtype is not None:
            # So the property can be seen for what it is
            return self
        value = obj.metadata.get(self.name)
        if value is None and self.variant_required and obj.variant in self.variant_required:
            raise AttributeError("{0} is missing from a mzTab-\"{1}\" document where it is required!".format(
                self.name, obj.variant))
        return value

    def __set__(self, obj, value):
        obj.metadata[self.name] = value

    def __delete__(self, obj):
        del obj.metadata[self.name]

    def build_docstring(self):
        doc = '''Accesses the {self.name!r} key in the :attr:`metadata` mapping attached
to this object.
'''
        if self.variant_required:
            if len(self.variant_required) > 1:
                plural = 's'
            else:
                plural = ''
            requires = ' or '.join(['-%s' % v for v in self.variant_required])
            doc += '''
This key must be present when the file is of {requires} variant{plural}.
        '''.format(requires=requires, plural=plural)
        doc += '''
Returns
-------
object
        '''
        doc = doc.format(self=self)
        return doc


class MetadataBackedCollection(object):
    def __init__(self, name, variant_required=None):
        self.name = name
        self.variant_required = variant_required
        self.__doc__ = self.build_docstring()

    def __get__(self, obj, objtype=None):
        if obj is None and objtype is not None:
            # So the property can be seen for what it is
            return self
        groups = obj.gather(obj.metadata)
        value = groups.get(self.name)
        if value is None and self.variant_required and obj.variant in self.variant_required:
            raise AttributeError("{0} is missing from a mzTab-\"{1}\" document where it is required!".format(
                self.name, obj.variant))
        return value

    def build_docstring(self):
        doc = '''Accesses the {self.name!r} key group gathered in the :attr:`metadata` mapping attached
to this object.

This group is dynamically generated on each access and may be expensive for repeated use.
'''
        if self.variant_required:
            if len(self.variant_required) > 1:
                plural = 's'
            else:
                plural = ''
            requires = ' or '.join(['-%s' % v for v in self.variant_required])
            doc += '''
This key must be present when the file is of {requires} variant{plural}.
        '''.format(requires=requires, plural=plural)
        doc += '''
Returns
-------
:class:`~.Group`
        '''
        doc = doc.format(self=self)
        return doc



class MetadataPropertyAnnotator(type):
    '''A simple metaclass to do some class-creation time introspection
    and descriptor binding.

    Uses a list of strings or 3-tuples from :attr:`__metadata_properties__` to
    bind :class:`MetadataBackedProperty` or :class:`MetadataBackedCollection`
    onto the class during its creation.

    The specification for a property is a tuple of three values:
        1. The metadata key to fetch
        2. The property name to expose on the object
        3. The variant(s) which require this metadata key be present

    :obj:`("mzTab-version", "version", ("M", "P"))` would be interpreted as
    Expose a property "version" on instances which serves the key "mzTab-version"
    from the instance's :attr:`metadata`, and raise an error if it is absent in
    the "M" or "P" variants.

    Alternatively a specification may be a single string which will be interpreted
    as the metadata key, and used to generate the property name replacing all '-'
    with '_' and assumed to be optional in all variants.

    If a metadata key ends with "[]" the property is assumed to be a collection. mzTab
    makes heavy use of "<collection_name>[<index>]..." keys to define groups of homogenous
    object types, often with per-element attributes.

    .. code-block::

        variable_mod[1]    CHEMMOD:15.9949146221
        variable_mod[1]-site  M
        variable_mod[1]-position    Anywhere
        variable_mod[2]    CHEMMOD:42.0105646863
        variable_mod[2]-site  N-term
        variable_mod[2]-position Protein N-term

    A specification :obj:`("variable_mod[]", "variable_mods", ())` would create a property
    that returns:

    .. code-block:: python

        >>>instance.variable_mods
        Group([(1,
                    {'name': 'CHEMMOD:15.9949146221',
                     'position': 'Anywhere',
                     'site': 'M'}),
                (2,
                    {'name': 'CHEMMOD:42.0105646863',
                     'position': 'Protein N-term',
                     'site': 'N-term'})])

    For precise description of the property collection algorithm, see
    :meth:`~_MzTabParserBase.collapse_properties` and
    :meth:`~_MzTabParserBase.gather`.

    If any base classes have a :attr:`__metadata_properties__` attribute, it will
    also be included unless :attr:`__inherit_metadata_properties__` is set to
    :const:`False`. Any names explicitly set by the current class override this
    automatic property generation.
    '''
    def __new__(mcls, name, bases, attrs):
        props = attrs.get('__metadata_properties__', [])
        inherit_props = attrs.get("__inherit_metadata_properties__", True)
        # Gather from parent classes so we can use inheritance for overriding this
        # behavior too.
        if inherit_props:
            for base in bases:
                props.extend(getattr(base, '__metadata_properties__', []))

        keys = set(attrs)

        # Iterate in reverse to ensure that classes nearer to the new classes override
        # more basal classes, ending with the new class to make sure overrides are
        # applied.
        for prop in reversed(props):
            # If the property definition is a single string, interpret the specification
            # as the property name, and apply some simple normalization to make it a valid
            # Python attribute name and assume the property is always optional.
            if isinstance(prop, str):
                prop_name = prop
                attr_name = prop_name.replace("mzTab-", '').replace('-', '_')
                variant_required = None
            else:
                # Otherwise unpack the triple
                prop_name, attr_name, variant_required = prop
            # Attach the new descriptor to the class definition to be created. These descriptors
            # will then be used when instances of that class try to get/set those attribute names.
            if attr_name in keys:
                continue
            if prop_name.endswith('[]'):
                # If the property name ends with "[]", then we're dealing with a collection so
                # use the :class:`MetadataBackedCollection` descriptor
                attrs[attr_name] = MetadataBackedCollection(
                    prop_name[:-2], variant_required=variant_required)
            else:
                # Otherwise it is a scalar-valued property, using the :class:`MetadataBackedProperty`
                # descriptor
                prop = attrs[attr_name] = MetadataBackedProperty(
                    prop_name, variant_required=variant_required)

        return super(MetadataPropertyAnnotator, mcls).__new__(mcls, name, bases, attrs)


class _MzTabParserBase(object):
    def _parse_param(self, tuplet):
        """Parse a controlled vocabulary or user specified parameter tuplet
        into a Python object

        Parameters
        ----------
        tuplet : str
            A square brace enclosed tuplet of values describing the parameter

        Returns
        -------
        tuple
            The reduced representation of the parameter
        """
        cv, acc, name, value = re.split(r"\s*,\s*", tuplet[1:-1])
        param_name = cvstr(name, acc)
        if value:
            return (param_name, value)
        else:
            return (param_name)

    def collapse_properties(self, proplist):
        '''Collapse a flat property list into a hierchical structure.

        This is intended to operate on :py:class:`Mapping` objects, including
        :class:`dict`, :class:`pandas.Series` and :class:`pandas.DataFrame`.

        .. code-block:: python

            {
              "ms_run[1]-format": "Andromeda:apl file format",
              "ms_run[1]-location": "file://...",
              "ms_run[1]-id_format": "scan number only nativeID format"
            }

        to

        .. code-block:: python

            {
              "ms_run": [
                {
                  "format": "Andromeda:apl file format",
                  "location": "file://...",
                  "id_format": "scan number only nativeID format"
                }
              ]
            }

        Parameters
        ----------
        proplist: :class:`Mapping`
            Key-Value pairs to collapse

        Returns
        -------
        :class:`OrderedDict`:
            The collapsed property list
        '''
        entities = OrderedDict()
        rest = {}
        for key, value in proplist.items():
            try:
                entity, prop_name = key.rsplit("-", 1)
            except ValueError:
                rest[key] = value
                continue
            try:
                entity_dict = entities[entity]
            except KeyError:
                entity_dict = entities[entity] = {}
            entity_dict[prop_name] = value
        for key, value in proplist.items():
            if key in entities:
                entity = entities[key]
                if 'name' not in entity:
                    entity['name'] = value
        for key, value in rest.items():
            if key in entities:
                entities[key]['name'] = value
            else:
                entities[key] = value
        return entities

    def _collapse_collections(self, entities):
        gathered = Group()
        for key, props in entities.items():
            if '[' in key:
                k, ix = key.split('[', 1)
                if '[' in ix:
                    # If we have multiple [ in a key, we are dealing with a path
                    path = extract_path(key)
                    for k, ix in path[:-1]:
                        store = gathered[k]
                        store = store[int(ix)]
                    k, ix = path[-1]
                    store[k][int(ix)] = props

                else:
                    ix = int(ix[:-1])
                    gathered[k][ix] = props
            else:
                gathered[key] = props
        return gathered

    def _cast_value(self, value):
        """Convert a cell value to the appropriate Python type

        Parameters
        ----------
        value : str
            The cell value as text

        Returns
        -------
        object
            The most specialized type recognized
        """
        if value == 'null':
            return None
        # is it a parameter?
        if value.startswith("["):
            try:
                if "|" in value:
                    return [self._cast_value(v) for v in value.split("|")]
                else:
                    return self._parse_param(value)
            except ValueError:
                return value
        else:
            # begin guessing dtype
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass
            return value

    def gather(self, mapping):
        '''Collapse property lists using :meth:`collapse_properties`
        and then gather collections of entites into lists.

        Parameters
        ----------
        mapping : dict
            The flattened hierarchy of properties to re-construct

        Returns
        -------
        Group :
            A :class:`Group` of all entities and collections of entities
        '''
        return self._collapse_collections(self.collapse_properties(mapping))


class _MzTabTable(_MzTabParserBase):

    """An internal class for accumulating information about an single table
    represented in an mzTab file

    Attributes
    ----------
    header : list
        The column names for the table
    name : str
        The table's name, human readable
    rows : list
        An accumulator of table rows
    """

    def __init__(self, name, header=None, rows=None):
        if rows is None:
            rows = []
        self.name = name
        self.header = header
        self.rows = rows

    def __repr__(self):
        n_cols = len(self.header) if self.header is not None else 0
        n_rows = len(self.rows)
        template = "<_MzTabTable {name} with {n_cols} columns and {n_rows} rows>"
        return template.format(n_cols=n_cols, n_rows=n_rows, name=self.name)

    def add(self, row):
        self.rows.append([self._cast_value(v) for v in row])

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        if isinstance(i, int):
            return self.gather({h: r for h, r in zip(self.header, self.rows[i])})
        elif isinstance(i, slice):
            out = []
            for i in range(i.start or 0, i.stop or len(self), i.step or 1):
                out.append(self[i])
            return out
        raise TypeError("Cannot access table with object of type %r" % type(i))

    def as_dict(self):
        return {"rows": [dict(zip(self.header, row)) for row in self.rows],
                "name": self.name}

    def as_df(self, index=None):
        """Convert the table to a DataFrame in memory.

        Returns
        -------
        pd.DataFrame
        """
        _require_pandas()
        table = pd.DataFrame(data=self.rows, columns=self.header)
        if index is not None and len(table.index) > 0:
            table = table.set_index(index, drop=False)
        table.name = self.name
        return table

    def clear(self):
        self.header = None
        self.rows = []


DATA_FRAME_FORMAT = 'df'
DICT_FORMAT = 'dict'
RAW_FORMAT = 'raw'

PATH_PARSER = re.compile(r"([^\[]+)\[(\d+)\]_?")


def extract_path(path):
    '''Parse `key[index]_next_key[next_index]...` sequences into
    lists of (key, index) pairs.

    Parameters
    ----------
    path : str
        The path key to parse

    Returns
    -------
    list
    '''
    return [(t, int(i)) for t, i in PATH_PARSER.findall(path)]


class Group(OrderedDict):
    '''A type for holding collections of arbitrarily nested keys from rows
    and metadata mappings.

    Implemented as an autovivifying :class:`OrderedDict` variant. As such implements
    the :class:`~collections.abc.Mapping` interface.
    '''

    def get_path(self, path, default=None):
        '''As :meth:`get` but over a path key parsed with :func:`extract_path`.

        Parameters
        ----------
        path : str
            The path to search down
        default : object, optional
            The return value when the path is missing

        Returns
        -------
        object
        '''
        tokens = extract_path(path)
        if not tokens:
            return self.get(path, default)
        layer = self
        for k, i in tokens[:-1]:
            i = int(i)
            layer = layer.get(k)
            if layer is None:
                return None
            layer = layer.get(i)
            if layer is None:
                return None
        k, i = tokens[-1]
        i = int(i)
        layer = layer.get(k)
        if layer is None:
            return default
        value = layer.get(i, default)
        return value

    def __missing__(self, key):
        value = self.__class__()
        self[key] = value
        return value


@add_metaclass(MetadataPropertyAnnotator)
class MzTab(_MzTabParserBase):
    """Parser for mzTab format files.

    Attributes
    ----------
    comments : list
        A list of comments across the file
    file : _file_obj
        A file stream wrapper for the file to be read
    metadata : OrderedDict
        A mapping of metadata that was entities.
    peptide_table : _MzTabTable or pd.DataFrame
        The table of peptides. Not commonly used.
    protein_table : _MzTabTable or pd.DataFrame
        The table of protein identifications.
    small_molecule_table : _MzTabTable or pd.DataFrame
        The table of small molecule identifications.
    spectrum_match_table : _MzTabTable or pd.DataFrame
        The table of spectrum-to-peptide match identifications.
    table_format: 'df', 'dict', or callable
        The structure type to replace each table with. The string
        'df' will use pd.DataFrame instances. 'dict' will create
        a dictionary of dictionaries for each table. A callable
        will be called on each raw _MzTabTable object
    """

    __metadata_properties__ = [
        ('mzTab-version', 'version', 'MP'),
        ('mzTab-mode', 'mode', 'P'),
        ('mzTab-type', 'type', 'P'),
        ('mzTab-ID', 'id', 'M'),
        'title',
        'description',
        ('ms_run[]', 'ms_runs', 'MP'),
        ('instrument[]', 'instruments', ()),
        ('software[]', 'software', ()),
        ('publication[]', 'publications', ()),
        ('contact[]', 'contacts', ()),
        ('uri[]', 'uris', ()),
        ('external_study_uri[]', 'external_study_uris', ()),
        ('quantification_method', 'quantification_method', 'M'),
        ('sample[]', 'samples', ()),
        ('assay[]', 'assays', ()),
        ('study_variable[]', 'study_variables', 'M'),
        ('custom[]', 'custom', ()),
        ('cv[]', 'cvs', 'M'),
        ('database[]', 'databases', 'M'),

        ('psm_search_engine_score[]', 'psm_search_engine_scores', ()),
        ('protein_search_engine_score[]', 'protein_search_engine_scores', ()),
        ('fixed_mod[]', 'fixed_mods', 'P'),
        ('variable_mod[]', 'variable_mods', 'P'),
        'colunit_protein',
        'colunit_peptide',
        'colunit_psm',
        'colunit_small_molecule',
        'false_discovery_rate',

        ('derivatization_agent[]', 'derivatization_agents', ()),
        ('small_molecule-quantification_unit',
         'small_molecule_quantification_unit', 'M'),
        ('small_molecule_feature-quantification_unit', 'small_molecule_feature_quantification_unit', 'M'),
        ('small_molecule-identification_reliability',
         'small_molecule_identification_reliability', ()),
        ('id_confidence_measure[]', 'id_confidence_measures', 'M'),
        ('colunit-small_molecule', 'colunit_small_molecule', ()),
        ('colunit-small_molecule_feature', 'colunit_small_molecule_feature', ()),
        ('colunit-small_molecule_evidence', 'colunit_small_molecule_evidence', ()),

        ('sample_processing[]', 'sample_processing', ())
    ]

    def __init__(self, path, encoding='utf8', table_format=DATA_FRAME_FORMAT):
        if table_format == DATA_FRAME_FORMAT:
            _require_pandas()
        self.file = _file_obj(path, mode='r', encoding=encoding)
        self.metadata = OrderedDict()
        self.comments = []
        self._table_format = table_format
        self._init_tables()
        self._parse()
        self._determine_schema_version()
        self._transform_tables()

    @property
    def table_format(self):
        return self._table_format

    def __getitem__(self, key):
        key = key.lower().strip()
        if key in ('psm', ):
            return self.spectrum_match_table
        if key in ('pep', ):
            return self.peptide_table
        if key in ('prt', ):
            return self.protein_table
        if key in ('sml', ):
            return self.small_molecule_table
        if key in ('smf', ):
            return self.small_molecule_feature_table
        if key in ('sme', ):
            return self.small_molecule_evidence_table
        else:
            raise KeyError(key)

    def __iter__(self):
        if self.variant == "P":
            yield 'PRT', self.protein_table
            yield 'PEP', self.peptide_table
            yield 'PSM', self.spectrum_match_table
            yield 'SML', self.small_molecule_table
        elif self.variant == "M":
            yield 'SML', self.small_molecule_table
            yield 'SMF', self.small_molecule_feature_table
            yield 'SME', self.small_molecule_evidence_table

    def _init_tables(self):
        self.protein_table = _MzTabTable("protein")
        self.peptide_table = _MzTabTable("peptide")
        self.spectrum_match_table = _MzTabTable('psm')
        self.small_molecule_table = _MzTabTable('small molecule')
        self.small_molecule_feature_table = _MzTabTable('small molecule feature')
        self.small_molecule_evidence_table = _MzTabTable('small molecule evidence')

    def _transform_tables(self):
        if self._table_format == DATA_FRAME_FORMAT:
            self.protein_table = self.protein_table.as_df('accession')
            self.peptide_table = self.peptide_table.as_df()
            self.spectrum_match_table = self.spectrum_match_table.as_df('PSM_ID')
            self.small_molecule_table = self.small_molecule_table.as_df()
            self.small_molecule_feature_table = self.small_molecule_feature_table.as_df()
            self.small_molecule_evidence_table = self.small_molecule_evidence_table.as_df()
        elif self._table_format in (DICT_FORMAT, dict):
            self.protein_table = self.protein_table.as_dict()
            self.peptide_table = self.peptide_table.as_dict()
            self.spectrum_match_table = self.spectrum_match_table.as_dict()
            self.small_molecule_table = self.small_molecule_table.as_dict()
            self.small_molecule_feature_table = self.small_molecule_feature_table.as_dict()
            self.small_molecule_evidence_table = self.small_molecule_evidence_table.as_dict()
        elif callable(self._table_format):
            self.protein_table = self._table_format(self.protein_table)
            self.peptide_table = self._table_format(self.peptide_table)
            self.spectrum_match_table = self._table_format(self.spectrum_match_table)
            self.small_molecule_table = self._table_format(self.small_molecule_table)
            self.small_molecule_feature_table = self._table_format(self.small_molecule_feature_table)
            self.small_molecule_evidence_table = self._table_format(self.small_molecule_evidence_table)

    def _parse(self):
        for i, line in enumerate(self.file):
            line = line.strip()
            tokens = line.split("\t")
            if not tokens:
                continue
            if tokens[0] == ("MTD"):
                name = tokens[1]
                value = self._cast_value(tokens[2])
                self.metadata[name] = value
            elif tokens[0] == 'COM':
                self.comments.append(self._cast_value(tokens[1]))
            # headers
            elif tokens[0] == "PRH":
                self.protein_table.header = tokens[1:]
            elif tokens[0] == "PEH":
                self.peptide_table.header = tokens[1:]
            elif tokens[0] == "PSH":
                self.spectrum_match_table.header = tokens[1:]
            elif tokens[0] == "SMH":
                self.small_molecule_table.header = tokens[1:]
            elif tokens[0] == "SFH":
                self.small_molecule_feature_table.header = tokens[1:]
            elif tokens[0] == "SEH":
                self.small_molecule_evidence_table.header = tokens[1:]
            # rows
            elif tokens[0] == "PRT":
                self.protein_table.add(tokens[1:])
            elif tokens[0] == "PEP":
                self.peptide_table.add(tokens[1:])
            elif tokens[0] == "PSM":
                self.spectrum_match_table.add(tokens[1:])
            elif tokens[0] == "SML":
                self.small_molecule_table.add(tokens[1:])
            elif tokens[0] == "SMF":
                self.small_molecule_feature_table.add(tokens[1:])
            elif tokens[0] == "SME":
                self.small_molecule_evidence_table.add(tokens[1:])

    def _determine_schema_version(self):
        version_parsed, variant = re.search(r"(?P<schema_version>\d+.\d+.\d+)(?:-(?P<schema_variant>[MP]))?", self.version).groups()
        if variant is None:
            variant = "P"
        self.num_version = [int(v) for v in version_parsed.split(".")]
        self.variant = variant

    def keys(self):
        return OrderedDict(list(self)).keys()

    def values(self):
        return OrderedDict(list(self)).values()

    def items(self):
        return OrderedDict(list(self)).items()
