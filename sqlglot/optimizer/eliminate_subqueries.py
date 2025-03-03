import itertools

from sqlglot import expressions as exp
from sqlglot.helper import find_new_name
from sqlglot.optimizer.scope import build_scope
from sqlglot.optimizer.simplify import simplify


def eliminate_subqueries(expression):
    """
    Rewrite subqueries as CTES, deduplicating if possible.

    Example:
        >>> import sqlglot
        >>> expression = sqlglot.parse_one("SELECT a FROM (SELECT * FROM x) AS y")
        >>> eliminate_subqueries(expression).sql()
        'WITH y AS (SELECT * FROM x) SELECT a FROM y AS y'

    This also deduplicates common subqueries:
        >>> expression = sqlglot.parse_one("SELECT a FROM (SELECT * FROM x) AS y JOIN (SELECT * FROM x) AS z")
        >>> eliminate_subqueries(expression).sql()
        'WITH y AS (SELECT * FROM x) SELECT a FROM y AS y JOIN y AS z'

    Args:
        expression (sqlglot.Expression): expression
    Returns:
        sqlglot.Expression: expression
    """
    if isinstance(expression, exp.Subquery):
        # It's possible to have subqueries at the root, e.g. (SELECT * FROM x) LIMIT 1
        eliminate_subqueries(expression.this)
        return expression

    expression = simplify(expression)
    root = build_scope(expression)

    # Map of alias->Scope|Table
    # These are all aliases that are already used in the expression.
    # We don't want to create new CTEs that conflict with these names.
    taken = {}

    # All CTE aliases in the root scope are taken
    for scope in root.cte_scopes:
        taken[scope.expression.parent.alias] = scope

    # All table names are taken
    for scope in root.traverse():
        taken.update({source.name: source for _, source in scope.sources.items() if isinstance(source, exp.Table)})

    # Map of Expression->alias
    # Existing CTES in the root expression. We'll use this for deduplication.
    existing_ctes = {}

    with_ = root.expression.args.get("with")
    if with_:
        for cte in with_.expressions:
            existing_ctes[cte.this] = cte.alias
    new_ctes = []

    # We're adding more CTEs, but we want to maintain the DAG order.
    # Derived tables within an existing CTE need to come before the existing CTE.
    for cte_scope in root.cte_scopes:
        # Append all the new CTEs from this existing CTE
        for scope in cte_scope.traverse():
            new_cte = _eliminate(scope, existing_ctes, taken)
            if new_cte:
                new_ctes.append(new_cte)

        # Append the existing CTE itself
        new_ctes.append(cte_scope.expression.parent)

    # Now append the rest
    for scope in itertools.chain(root.union_scopes, root.subquery_scopes, root.derived_table_scopes):
        for child_scope in scope.traverse():
            new_cte = _eliminate(child_scope, existing_ctes, taken)
            if new_cte:
                new_ctes.append(new_cte)

    if new_ctes:
        expression.set("with", exp.With(expressions=new_ctes))

    return expression


def _eliminate(scope, existing_ctes, taken):
    if scope.is_union:
        return _eliminate_union(scope, existing_ctes, taken)

    if scope.is_derived_table and not isinstance(scope.expression, (exp.Unnest, exp.Lateral)):
        return _eliminate_derived_table(scope, existing_ctes, taken)


def _eliminate_union(scope, existing_ctes, taken):
    duplicate_cte_alias = existing_ctes.get(scope.expression)

    alias = duplicate_cte_alias or find_new_name(taken=taken, base="cte")

    taken[alias] = scope

    # Try to maintain the selections
    expressions = scope.expression.args.get("expressions")
    selects = [
        exp.alias_(exp.column(e.alias_or_name, table=alias), alias=e.alias_or_name)
        for e in expressions
        if e.alias_or_name
    ]
    # If not all selections have an alias, just select *
    if len(selects) != len(expressions):
        selects = ["*"]

    scope.expression.replace(exp.select(*selects).from_(exp.alias_(exp.table_(alias), alias=alias)))

    if not duplicate_cte_alias:
        existing_ctes[scope.expression] = alias
        return exp.CTE(
            this=scope.expression,
            alias=exp.TableAlias(this=exp.to_identifier(alias)),
        )


def _eliminate_derived_table(scope, existing_ctes, taken):
    duplicate_cte_alias = existing_ctes.get(scope.expression)
    parent = scope.expression.parent
    name = alias = parent.alias

    if not alias:
        name = alias = find_new_name(taken=taken, base="cte")

    if duplicate_cte_alias:
        name = duplicate_cte_alias
    elif taken.get(alias):
        name = find_new_name(taken=taken, base=alias)

    taken[name] = scope

    table = exp.alias_(exp.table_(name), alias=alias)
    parent.replace(table)

    if not duplicate_cte_alias:
        existing_ctes[scope.expression] = name
        return exp.CTE(
            this=scope.expression,
            alias=exp.TableAlias(this=exp.to_identifier(name)),
        )
