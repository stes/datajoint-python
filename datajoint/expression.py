from itertools import count
import logging
import inspect
import copy
import re
from .settings import config
from .errors import DataJointError
from .fetch import Fetch, Fetch1
from .preview import preview, repr_html
from .condition import AndList, Not, \
    make_condition, assert_join_compatibility, extract_column_names, PromiscuousOperand

logger = logging.getLogger(__name__)


class QueryExpression:
    """
    QueryExpression implements query operators to derive new entity set from its input.
    A QueryExpression object generates a SELECT statement in SQL.
    QueryExpression operators are restrict, join, proj, aggr, and union.

    A QueryExpression object has a support, a restriction (an AndList), and heading.
    Property `heading` (type dj.Heading) contains information about the attributes.
    It is loaded from the database and updated by proj.

    Property `support` is the list of table names or other QueryExpressions to be joined.

    The restriction is applied first without having access to the attributes generated by the projection.
    Then projection is applied by selecting modifying the heading attribute.

    Application of operators does not always lead to the creation of a subquery.
    A subquery is generated when:
        1. A restriction is applied on any computed or renamed attributes
        2. A projection is applied remapping remapped attributes
        3. Subclasses: Join, Aggregation, and Union have additional specific rules.
    """

    _restriction = None
    _restriction_attributes = None

    # subclasses or instantiators must provide values
    _connection = None
    _heading = None
    _support = None
    _join_attributes = []

    @property
    def connection(self):
        """ a dj.Connection object """
        assert self._connection is not None
        return self._connection

    @property
    def support(self):
        """ A list of table names or subqueries to from the FROM clause """
        assert self._support is not None
        return self._support

    @property
    def heading(self):
        """ a dj.Heading object, reflects the effects of the projection operator .proj """
        return self._heading

    @property
    def restriction(self):
        """ a AndList object of restrictions applied to input to produce the result """
        if self._restriction is None:
            self._restriction = AndList()
        return self._restriction
    
    @property
    def restriction_attributes(self):
        """ the set of names invoked in the WHERE clause """
        if self._restriction_attributes is None:
            self._restriction_attributes = set()
        return self._restriction_attributes

    @property
    def primary_key(self):
        return self.heading.primary_key

    __subquery_alias_count = count()    # count for alias names used in from_clause

    @property
    def from_clause(self):
        support = ('(' + src.make_sql() + ') as `_s%x`' % next(
            self.__subquery_alias_count) if isinstance(src, QueryExpression) else src for src in self.support)
        clause = next(support)
        for s, a in zip(support, self._join_attributes):
            clause += ' JOIN {clause}{using}'.format(
                clause=s,
                using="" if not a else " USING (%s)" % ",".join('`%s`' % _ for _ in a))
        return clause

    @property
    def where_clause(self):
        return '' if not self.restriction else ' WHERE(%s)' % ')AND('.join(str(s) for s in self.restriction)

    def make_sql(self, fields=None):
        """
        Make the SQL SELECT statement.
        :param fields: used to explicitly set the select attributes
        """
        distinct = self.heading.names == self.primary_key
        return 'SELECT {distinct}{fields} FROM {from_}{where}'.format(
            distinct="DISTINCT " if distinct else "",
            fields=self.heading.as_sql(fields or self.heading.names),
            from_=self.from_clause, where=self.where_clause)

    # --------- query operators -----------
    def make_subquery(self):
        """ create a new SELECT statement where self is the FROM clause """
        result = QueryExpression()
        result._connection = self.connection
        result._support = [self]
        result._heading = self.heading.make_subquery_heading()
        return result

    def restrict(self, restriction):
        attributes = set()
        new_condition = make_condition(self, restriction, attributes)
        if new_condition is True:
            return self  # restriction has no effect, return the same object
        # check that all attributes in condition are present in the query
        try:
            raise DataJointError("Attribute `%s` is not found in query." % next(
                attr for attr in attributes if attr not in self.heading.names))
        except StopIteration:
            pass  # all ok
        # If the new condition uses any new attributes, a subquery is required.
        # However, Aggregation's HAVING statement works fine with aliased attributes.
        need_subquery = isinstance(self, Union) or (
                not isinstance(self, Aggregation) and self.heading.new_attributes)
        if need_subquery:
            result = self.make_subquery()
        else:
            result = copy.copy(self)
            result._restriction = AndList(self.restriction)   # make a copy to protect the original
        result.restriction.append(new_condition)
        result.restriction_attributes.update(attributes)
        return result

    def restrict_in_place(self, restriction):
        self.__dict__.update(self.restrict(restriction).__dict__)

    def __and__(self, restriction):
        """
        Restriction operator
        :return: a restricted copy of the input argument
        See QueryExpression.restrict for more detail.
        """
        return self.restrict(restriction)

    def __xor__(self, restriction):
        """
        Restriction operator ignoring compatibility check.
        """
        if inspect.isclass(restriction) and issubclass(restriction, QueryExpression):
            restriction = restriction()
        if isinstance(restriction, Not):
            return self.restrict(Not(PromiscuousOperand(restriction.restriction)))
        return self.restriction(PromiscuousOperand(restriction))

    def __sub__(self, restriction):
        """
        Inverted restriction
        :return: a restricted copy of the input argument
        See QueryExpression.restrict for more detail.
        """
        return self.restrict(Not(restriction))

    def __neg__(self, restriction):
        if isinstance(restriction, Not):
            return restriction.restriction
        return Not(restriction)

    def __mul__(self, other):
        """ join of query expressions `self` and `other` """
        if inspect.isclass(other) and issubclass(other, QueryExpression):
            other = other()  # instantiate
        if isinstance(other, U):
            return other * self
        return self._join(other)

    def __matmul__(self, other):
        if inspect.isclass(other) and issubclass(other, QueryExpression):
            other = other()  # instantiate
        if not isinstance(other, QueryExpression):
            raise DataJointError("The argument of join must be a QueryExpression")
        return self._join(other, semantic_check=False)

    def _join(self, other, semantic_check=True):
        """create the joined QueryExpression"""
        # trigger subqueries if joining on renamed attributes
        other_clash = set(other.heading.names) | set(
            (other.heading[n].attribute_expression.strip('`') for n in other.heading.new_attributes))
        self_clash = set(self.heading.names) | set(
            (self.heading[n].attribute_expression for n in self.heading.new_attributes))
        need_subquery1 = isinstance(self, Union) or any(
            n for n in self.heading.new_attributes if (
                    n in other_clash or self.heading[n].attribute_expression.strip('`') in other_clash))
        need_subquery2 = isinstance(self, Union) or any(
            n for n in other.heading.new_attributes if (
                    n in self_clash or other.heading[n].attribute_expression.strip('`') in other_clash))
        if need_subquery1:
            self = self.make_subquery()
        if need_subquery2:
            other = other.make_subquery()
        if semantic_check:
            assert_join_compatibility(self, other)
        result = QueryExpression()
        result._connection = self.connection
        result._support = self.support + other.support
        result._join_attributes = self._join_attributes + [[a for a in self.heading.names if a in other.heading.names]]
        result._heading = self.heading.join(other.heading)
        result._restriction = AndList(self.restriction)
        result._restriction.append(other.restriction)
        assert len(result.support) == len(result._join_attributes) + 1
        return result

    def __add__(self, other):
        """union"""
        return Union.create(self, other)

    def proj(self, *attributes, **named_attributes):
        """
        Projection operator.
        :param attributes:  attributes to be included in the result. (The primary key is already included).
        :param named_attributes: new attributes computed or renamed from existing attributes.
        :return: the projected expression.
        Primary key attributes cannot be excluded but may be renamed.
        If the attribute list contains an Ellipsis ..., then all secondary attributes are included too
        Prefixing an attribute name with a dash '-attr' removes the attribute from the list if present.
        Keyword arguments can be used to rename attributes as in name='attr', duplicate them as in name='(attr)', or
        self.proj(...) or self.proj(Ellipsis) -- include all attributes (return self)
        self.proj() -- include only primary key
        self.proj('attr1', 'attr2')  -- include primary key and attributes attr1 and attr2
        self.proj(..., '-attr1', '-attr2')  -- include attributes except attr1 and attr2
        self.proj(name1='attr1') -- include primary key and 'attr1' renamed as name1
        self.proj('attr1', dup='(attr1)') -- include primary key and attribute attr1 twice, with the duplicate 'dup'
        self.proj(k='abs(attr1)') adds the new attribute k with the value computed as an expression (SQL syntax)
        from other attributes available before the projection.
        Each attribute name can only be used once.
        """
        # new attributes in parentheses are included again with the new name without removing original
        duplication_pattern = re.compile(r'\s*\(\s*(?P<name>[a-z][a-z_0-9]*)\s*\)\s*$')
        # attributes without parentheses renamed
        rename_pattern = re.compile(r'\s*(?P<name>[a-z][a-z_0-9]*)\s*$')
        replicate_map = {k: m.group('name')
                         for k, m in ((k, duplication_pattern.match(v)) for k, v in named_attributes.items()) if m}
        rename_map = {k: m.group('name')
                      for k, m in ((k, rename_pattern.match(v)) for k, v in named_attributes.items()) if m}
        compute_map = {k: v for k, v in named_attributes.items()
                       if not duplication_pattern.match(v) and not rename_pattern.match(v)}
        attributes = set(attributes)
        # include primary key
        attributes.update((k for k in self.primary_key if k not in rename_map.values()))
        # include all secondary attributes with Ellipsis
        if Ellipsis in attributes:
            attributes.discard(Ellipsis)
            attributes.update((a for a in self.heading.secondary_attributes
                               if a not in attributes and a not in rename_map.values()))
        try:
            raise DataJointError("%s is not a valid data type for an attribute in .proj" % next(
                a for a in attributes if not isinstance(a, str)))
        except StopIteration:
            pass  # normal case
        # remove excluded attributes, specified as `-attr'
        excluded = set(a for a in attributes if a.strip().startswith('-'))
        attributes.difference_update((excluded))
        excluded = set(a.lstrip('-').strip() for a in excluded)
        attributes.difference_update(excluded)
        try:
            raise DataJointError("Cannot exclude primary key attribute %s", next(
                a for a in excluded if a in self.primary_key))
        except StopIteration:
            pass  # all ok
        # check that all attributes exist in heading
        try:
            raise DataJointError(
                'Attribute `%s` not found.' % next(a for a in attributes if a not in self.heading.names))
        except StopIteration:
            pass  # all ok

        # check that all mentioned names are present in heading
        mentions = attributes.union(replicate_map.values()).union(rename_map.values())
        try:
            raise DataJointError("Attribute '%s' not found." % next(a for a in mentions if not self.heading.names))
        except StopIteration:
            pass  # all ok

        # check that newly created attributes do not clash with any other selected attributes
        try:
            raise DataJointError("Attribute `%s` already exists" % next(
                a for a in rename_map if a in attributes.union(compute_map).union(replicate_map)))
        except StopIteration:
            pass  # all ok
        try:
            raise DataJointError("Attribute `%s` already exists" % next(
                a for a in compute_map if a in attributes.union(rename_map).union(replicate_map)))
        except StopIteration:
            pass  # all ok
        try:
            raise DataJointError("Attribute `%s` already exists" % next(
                a for a in replicate_map if a in attributes.union(rename_map).union(compute_map)))
        except StopIteration:
            pass  # all ok

        # need a subquery if the projection remaps any remapped attributes
        used = set(q for v in compute_map.values() for q in extract_column_names(v))
        used.update(rename_map.values())
        used.update(replicate_map.values())
        used.intersection_update(self.heading.names)
        need_subquery = isinstance(self, Union) or any(
            self.heading[name].attribute_expression is not None for name in used)
        if not need_subquery and self.restriction:
            # need a subquery if the restriction applies to attributes that have been renamed
            need_subquery = any(
                self.heading[name].attribute_expression is not None for name in self.restriction_attributes)

        result = self.make_subquery() if need_subquery else copy.copy(self)
        result._heading = result.heading.select(
            attributes, rename_map=dict(**rename_map, **replicate_map), compute_map=compute_map)
        return result

    def aggr(self, group, *attributes, keep_all_rows=False, **named_attributes):
        """
        Aggregation of the type U('attr1','attr2').aggr(group, computation="QueryExpression")
        has the primary key ('attr1','attr2') and performs aggregation computations for all matching elements of `group`.
        :param group:  The query expression to be aggregated.
        :keep_all_rows: True=keep all the rows from self. False=keep only rows that match entries in group.
        :param named_attributes: computations of the form new_attribute="sql expression on attributes of group"
        :return: The derived query expression
        """
        return Aggregation.create(
            self, group=group, keep_all_rows=keep_all_rows).proj(*attributes, **named_attributes)

    aggregate = aggr  # alias for aggr

    # ---------- Fetch operators --------------------
    @property
    def fetch1(self):
        return Fetch1(self)

    @property
    def fetch(self):
        return Fetch(self)

    def head(self, limit=25, **fetch_kwargs):
        """
        shortcut to fetch the first few entries from query expression.
        Equivalent to fetch(order_by="KEY", limit=25)
        :param limit:  number of entries
        :param fetch_kwargs: kwargs for fetch
        :return: query result
        """
        return self.fetch(order_by="KEY", limit=limit, **fetch_kwargs)

    def tail(self, limit=25, **fetch_kwargs):
        """
        shortcut to fetch the last few entries from query expression.
        Equivalent to fetch(order_by="KEY DESC", limit=25)[::-1]
        :param limit:  number of entries
        :param fetch_kwargs: kwargs for fetch
        :return: query result
        """
        return self.fetch(order_by="KEY DESC", limit=limit, **fetch_kwargs)[::-1]

    def __len__(self):
        """ :return: number of elements in the result set """
        what = '*' if set(self.heading.names) != set(self.primary_key) else 'DISTINCT %s' % ','.join(
            (self.heading[k].attribute_expression or '`%s`' % k for k in self.primary_key))
        return self.connection.query(
            'SELECT count({what}) FROM {from_}{where}'.format(
                what=what,
                from_=self.from_clause,
                where=self.where_clause)).fetchone()[0]

    def __bool__(self):
        """
        :return:  True if the result is not empty. Equivalent to len(rel)>0 but may be more efficient.
        """
        return len(self) > 0

    def __contains__(self, item):
        """
        returns True if item is found in the .
        :param item: any restriction
        (item in query_expression) is equivalent to bool(query_expression & item) but may be executed more efficiently.
        """
        return bool(self & item)  # May be optimized e.g. using an EXISTS query

    def __iter__(self):
        self._iter_only_key = all(v.in_key for v in self.heading.attributes.values())
        self._iter_keys = self.fetch('KEY')
        return self

    def __next__(self):
        try:
            key = self._iter_keys.pop(0)
        except AttributeError:
            # self._iter_keys is missing because __iter__ has not been called.
            raise TypeError("'QueryExpression' object is not an iterator. Use iter(obj) to create an iterator.")
        except IndexError:
            raise StopIteration
        else:
            if self._iter_only_key:
                return key
            else:
                try:
                    return (self & key).fetch1()
                except DataJointError:
                    # The data may have been deleted since the moment the keys were fetched -- move on to next entry.
                    return next(self)

    def cursor(self, offset=0, limit=None, order_by=None, as_dict=False):
        """
        See expression.fetch() for input description.
        :return: query cursor
        """
        if offset and limit is None:
            raise DataJointError('limit is required when offset is set')
        sql = self.make_sql()
        if order_by is not None:
            sql += ' ORDER BY ' + ', '.join(order_by)
        if limit is not None:
            sql += ' LIMIT %d' % limit + (' OFFSET %d' % offset if offset else "")
        logger.debug(sql)
        return self.connection.query(sql, as_dict=as_dict)

    def __repr__(self):
        return super().__repr__() if config['loglevel'].lower() == 'debug' else self.preview()

    def preview(self, limit=None, width=None):
        """ :return: a string of preview of the contents of the query. """
        return preview(self, limit, width)

    def _repr_html_(self):
        """ :return: HTML to display table in Jupyter notebook. """
        return repr_html(self)


class Aggregation(QueryExpression):
    """
    Aggregation(rel, comp1='expr1', ..., compn='exprn')  yields an entity set with the primary key specified by rel.heading.
    The computed arguments comp1, ..., compn use aggregation operators on the attributes of rel.
    Aggregation is used QueryExpression.aggr and U.aggr.
    Aggregation is a private class in DataJoint, not exposed to users.
    """
    _keep_all_rows = False
    _left_restrict = None   # the pre-GROUP BY conditions for the WHERE clause
    __subquery_alias_count = count()

    @classmethod
    def create(cls, arg, group, keep_all_rows=False):
        if inspect.isclass(group) and issubclass(group, QueryExpression):
            group = group()   # instantiate if a class
        assert isinstance(group, QueryExpression)
        if keep_all_rows and len(group.support) > 1:
            group = group.make_subquery()  # subquery if left joining a join
        join = arg * group  # reuse the join logic
        result = cls()
        result._connection = join.connection
        result._heading = join.heading.set_primary_key(arg.primary_key)  # use left operand's primary key
        result._support = join.support
        result._join_attributes = join._join_attributes
        result.initial_restriction = join.restriction  # before GROUP BY
        result._grouping_attributes = result.primary_key
        result._keep_all_rows = keep_all_rows
        return result

    @property
    def from_clause(self):
        support = ('(' + src.make_sql() + ') as `_s%x`' % next(
            self.__subquery_alias_count) if isinstance(src, QueryExpression) else src for src in self.support)
        clause = next(support)
        for s, a, i in zip(support, self._join_attributes, count(2)):
            clause += '{left} JOIN {clause}{using}'.format(
                left=" LEFT" if i == len(self.support) and self._keep_all_rows else "",
                clause=s,
                using="" if not a else " USING (%s)" % ",".join('`%s`' % _ for _ in a))
        return clause

    def make_sql(self, fields=None):
        where = '' if not self._left_restrict else ' WHERE (%s)' % ')AND('.join(self._left_restrict)
        fields = self.heading.as_sql(fields or self.heading.names)
        assert self._grouping_attributes or not self.restriction
        distinct = set(self.heading.names) == set(self.primary_key)
        return 'SELECT {distinct}{fields} FROM {from_}{where}{group_by}'.format(
            distinct="DISTINCT " if distinct else "",
            fields=fields,
            from_=self.from_clause,
            where=where,
            group_by="" if not self.primary_key else (
                " GROUP BY `%s`" % '`,`'.join(self._grouping_attributes) +
                ("" if not self.restriction else ' HAVING (%s)' % ')AND('.join(self.restriction))))

    def __len__(self):
        what = '*' if set(self.heading.names) != set(self.primary_key) else 'DISTINCT `%s`' % '`,`'.join(self.primary_key)
        return self.connection.query(
            'SELECT count({what}) FROM ({subquery}) as `_r{alias:x}`'.format(
                what=what,
                subquery=self.make_sql(),
                alias=next(self.__subquery_alias_count))).fetchone()[0]


class Union(QueryExpression):
    """
    Union is the private DataJoint class that implements the union operator.
    """
    @classmethod
    def create(cls, arg1, arg2):
        if inspect.isclass(arg2) and issubclass(arg2, QueryExpression):
            arg2 = arg2()  # instantiate if a class
        if not isinstance(arg2, QueryExpression):
            raise DataJointError('A QueryExpression can only be unioned with another QueryExpression')
        if arg1.connection != arg2.connection:
            raise DataJointError("Cannot operate on QueryExpressions originating from different connections.")
        if set(arg1.primary_key) != set(arg2.primary_key):
            raise DataJointError("The operands of a union must share the same primary key.")
        if set(arg1.heading.secondary_attributes) & set(arg2.heading.secondary_attributes):
            raise DataJointError("The operands of a union must not share any secondary attributes.")
        result = cls()
        result._connection = arg1.connection
        result._heading = arg1.heading
        result._support = [arg1, arg2]
        return result

    def make_sql(self, select_fields=None):
        arg1, arg2 = self._support
        if not arg1.heading.secondary_attributes and not arg2.heading.secondary_attributes:  # use UNION DISTINCT
            fields = arg1.primary_key
            return "({sql1}) UNION ({sql2})".format(sql1=arg1.make_sql(fields), sql2=arg2.make_sql(fields))


class U:
    """
    dj.U objects are the universal sets representing all possible values of their attributes.
    dj.U objects cannot be queried on their own but are useful for forming some queries.
    dj.U('attr1', ..., 'attrn') represents the universal set with the primary key attributes attr1 ... attrn.
    The universal set is the set of all possible combinations of values of the attributes.
    Without any attributes, dj.U() represents the set with one element that has no attributes.

    Restriction:

    dj.U can be used to enumerate unique combinations of values of attributes from other expressions.

    The following expression yields all unique combinations of contrast and brightness found in the `stimulus` set:

    >>> dj.U('contrast', 'brightness') & stimulus

    Aggregation:

    In aggregation, dj.U is used for summary calculation over an entire set:

    The following expression yields one element with one attribute `s` containing the total number of elements in
    query expression `expr`:

    >>> dj.U().aggr(expr, n='count(*)')

    The following expressions both yield one element containing the number `n` of distinct values of attribute `attr` in
    query expressio `expr`.

    >>> dj.U().aggr(expr, n='count(distinct attr)')
    >>> dj.U().aggr(dj.U('attr').aggr(expr), 'n=count(*)')

    The following expression yields one element and one attribute `s` containing the sum of values of attribute `attr`
    over entire result set of expression `expr`:

    >>> dj.U().aggr(expr, s='sum(attr)')

    The following expression yields the set of all unique combinations of attributes `attr1`, `attr2` and the number of
    their occurrences in the result set of query expression `expr`.

    >>> dj.U(attr1,attr2).aggr(expr, n='count(*)')

    Joins:

    If expression `expr` has attributes 'attr1' and 'attr2', then expr * dj.U('attr1','attr2') yields the same result
    as `expr` but `attr1` and `attr2` are promoted to the the primary key.  This is useful for producing a join on
    non-primary key attributes.
    For example, if `attr` is in both expr1 and expr2 but not in their primary keys, then expr1 * expr2 will throw
    an error because in most cases, it does not make sense to join on non-primary key attributes and users must first
    rename `attr` in one of the operands.  The expression dj.U('attr') * rel1 * rel2 overrides this constraint.
    """

    def __init__(self, *primary_key):
        self._primary_key = primary_key

    @property
    def primary_key(self):
        return self._primary_key

    def __and__(self, other):
        if inspect.isclass(other) and issubclass(other, QueryExpression):
            other = other()   # instantiate if a class
        if not isinstance(other, QueryExpression):
            raise DataJointError('Set U can only be restricted with a QueryExpression.')
        result = copy.copy(other)
        result._heading = result.heading.set_primary_key(self.primary_key)
        result = result.proj()
        return result

    def __mul__(self, other):
        """
        Joining U with a query expression has the effect of promoting the attributes of U to the primary key of
        the other query expression.
        :param other: the other query expression to join with.
        :return: a copy of the other query expression with the primary key extended.
        """
        if inspect.isclass(other) and issubclass(other, QueryExpression):
            other = other()   # instantiate if a class
        if not isinstance(other, QueryExpression):
            raise DataJointError('Set U can only be joined with a QueryExpression.')
        try:
            raise DataJointError(
                'Attribute `%s` not found' % next(k for k in self.primary_key if k not in other.heading.names))
        except StopIteration:
            pass  # all ok
        result = copy.copy(other)
        result._heading = result.heading.set_primary_key(
            other.primary_key + [k for k in self.primary_key if k not in other.primary_key])
        return result

    def aggr(self, group, **named_attributes):
        """
        Aggregation of the type U('attr1','attr2').aggr(group, computation="QueryExpression")
        has the primary key ('attr1','attr2') and performs aggregation computations for all matching elements of `group`.
        :param group:  The query expression to be aggregated.
        :param named_attributes: computations of the form new_attribute="sql expression on attributes of group"
        :return: The derived query expression
        """
        if named_attributes.get('keep_all_rows', False):
            raise DataJointError('Cannot set keep_all_rows=True when aggregating on a universal set.')
        return Aggregation.create(self, group=group,  keep_all_rows=False).proj(**named_attributes)

    aggregate = aggr  # alias for aggr
