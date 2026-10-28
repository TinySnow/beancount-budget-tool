"""Application query boundary: inputs, facts, proofs, presentation, verification.

The caller owns one session for a month or a range. Generated documents and
identity joins are shared for its lifetime; report decisions are never kept
between sessions. Compatibility CLI functions delegate here.
"""

from contextlib import ExitStack
from copy import deepcopy

from .errors import BudgetError, require
from .financial import financial_session
from .yamlstrict import load_yaml, yaml_path_location
from .projection import projection_scope


class QuerySession:
    def __init__(self, *, source_cache=None):
        self.source_cache = source_cache
        self._stack = None

    def __enter__(self):
        from .v6.facts import frozen_fact_indices
        if self._stack is not None:
            raise RuntimeError('QuerySession is already active')
        stack = ExitStack()
        try:
            stack.enter_context(financial_session(source_cache=self.source_cache))
            stack.enter_context(frozen_fact_indices())
            self.graph = stack.enter_context(projection_scope())
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(self, *exception):
        try:
            return self._stack.__exit__(*exception)
        finally:
            self._stack = None

    def month(self, config, month=None, *, include_transactions=False):
        if self._stack is None:
            raise RuntimeError('QuerySession must be used as a context manager')
        from .v6.monthly_report import audit_month_workflow
        from .v6.simple_config import build_runtime
        runtime = build_runtime(config, month, materialize=False)
        try:
            result = runtime.pending or audit_month_workflow(runtime.config, include_transactions=include_transactions)
            runtime.check()
            return runtime.public_sources(result)
        except BudgetError as exc:
            if exc.source and runtime.config and exc.source.startswith(str(runtime.config.parent)):
                raise BudgetError(exc.code, exc.message, yaml_path_location(config, ())) from exc
            raise
        finally:
            runtime.close()

    def period(self, config, since=None, through=None):
        if self._stack is None:
            raise RuntimeError('QuerySession must be used as a context manager')
        from .v6.period_report import _months, _combine_months, audit_period
        from .v6.simple_config import build_runtime
        from .v6.targets import _month
        require((since is None) == (through is None), 'PERIOD_RANGE_INVALID', '起止月份须同时填写')
        document = load_yaml(config)
        first, last = _month(since or document['cutover'][:7]), _month(through or document['month'])
        require(first <= last, 'PERIOD_RANGE_INVALID', '起始月份不能晚于结束月份')
        months = _months(first, last)
        runtimes = []
        try:
            for month in months:
                runtimes.append(build_runtime(config, month, materialize=False))
            if any(runtime.pending for runtime in runtimes):
                rows = [{'month': runtime.month, 'config': str(config),
                         'status': 'NEEDS_REVIEW' if runtime.pending else 'NOT_RUN'} for runtime in runtimes]
                reviews = [{**item, 'month': runtime.month, 'config': str(config)} for runtime in runtimes
                           if runtime.pending for item in runtime.pending['review_items']]
                result = _combine_months(months, rows, [], reviews, set(), [])
            else:
                result = audit_period([runtime.config for runtime in runtimes], first, last)
            result = deepcopy(result)
            for runtime in runtimes:
                runtime.check()
                runtime.rewrite_sources(result)
            return result
        finally:
            for runtime in runtimes:
                runtime.close()
