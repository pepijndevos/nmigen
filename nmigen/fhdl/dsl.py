from collections import OrderedDict
from contextlib import contextmanager

from .ast import *
from .ir import *
from .xfrm import *


__all__ = ["Module", "SyntaxError"]


class SyntaxError(Exception):
    pass


class _ModuleBuilderProxy:
    def __init__(self, builder, depth):
        object.__setattr__(self, "_builder", builder)
        object.__setattr__(self, "_depth", depth)


class _ModuleBuilderDomain(_ModuleBuilderProxy):
    def __init__(self, builder, depth, cd_name):
        super().__init__(builder, depth)
        self._cd_name = cd_name

    def __iadd__(self, assigns):
        self._builder._add_statement(assigns, cd_name=self._cd_name, depth=self._depth)
        return self


class _ModuleBuilderDomains(_ModuleBuilderProxy):
    def __getattr__(self, name):
        if name == "comb":
            cd_name = None
        else:
            cd_name = name
        return _ModuleBuilderDomain(self._builder, self._depth, cd_name)

    def __getitem__(self, name):
        return self.__getattr__(name)

    def __setattr__(self, name, value):
        if name == "_depth":
            object.__setattr__(self, name, value)
        elif not isinstance(value, _ModuleBuilderDomain):
            raise AttributeError("Cannot assign 'd.{}' attribute; did you mean 'd.{} +='?"
                                 .format(name, name))

    def __setitem__(self, name, value):
        return self.__setattr__(name, value)


class _ModuleBuilderRoot:
    def __init__(self, builder, depth):
        self._builder = builder
        self.domain = self.d = _ModuleBuilderDomains(builder, depth)

    def __getattr__(self, name):
        if name in ("comb", "sync"):
            raise AttributeError("'{}' object has no attribute '{}'; did you mean 'd.{}'?"
                                 .format(type(self).__name__, name, name))
        raise AttributeError("'{}' object has no attribute '{}'"
                             .format(type(self).__name__, name))


class _ModuleBuilderCase(_ModuleBuilderRoot):
    def __init__(self, builder, depth, test, value):
        super().__init__(builder, depth)
        self._test  = test
        self._value = value

    def __enter__(self):
        if self._value is None:
            self._value = "-" * len(self._test)
        if isinstance(self._value, str) and len(self._test) != len(self._value):
            raise SyntaxError("Case value {} must have the same width as test {}"
                              .format(self._value, self._test))
        if self._builder._stmt_switch_test != ValueKey(self._test):
            self._builder._flush()
            self._builder._stmt_switch_test = ValueKey(self._test)
        self._outer_case = self._builder._statements
        self._builder._statements = []
        return self

    def __exit__(self, *args):
        self._builder._stmt_switch_cases[self._value] = self._builder._statements
        self._builder._statements = self._outer_case


class _ModuleBuilderSubmodules:
    def __init__(self, builder):
        object.__setattr__(self, "_builder", builder)

    def __iadd__(self, submodules):
        for submodule in submodules:
            self._builder._add_submodule(submodule)
        return self

    def __setattr__(self, name, submodule):
        self._builder._add_submodule(submodule, name)


class Module(_ModuleBuilderRoot):
    def __init__(self):
        _ModuleBuilderRoot.__init__(self, self, depth=0)
        self.submodules = _ModuleBuilderSubmodules(self)

        self._submodules        = []
        self._driving           = ValueDict()
        self._statements        = Statement.wrap([])
        self._stmt_depth        = 0
        self._stmt_if_cond      = []
        self._stmt_if_bodies    = []
        self._stmt_switch_test  = None
        self._stmt_switch_cases = OrderedDict()

    @contextmanager
    def If(self, cond):
        self._flush()
        try:
            _outer_case = self._statements
            self._statements = []
            self.domain._depth += 1
            yield
            self._stmt_if_cond.append(cond)
            self._stmt_if_bodies.append(self._statements)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case

    @contextmanager
    def Elif(self, cond):
        if not self._stmt_if_cond:
            raise SyntaxError("Elif without preceding If")
        try:
            _outer_case = self._statements
            self._statements = []
            self.domain._depth += 1
            yield
            self._stmt_if_cond.append(cond)
            self._stmt_if_bodies.append(self._statements)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case

    @contextmanager
    def Else(self):
        if not self._stmt_if_cond:
            raise SyntaxError("Else without preceding If/Elif")
        try:
            _outer_case = self._statements
            self._statements = []
            self.domain._depth += 1
            yield
            self._stmt_if_bodies.append(self._statements)
        finally:
            self.domain._depth -= 1
            self._statements = _outer_case
        self._flush()

    def Case(self, test, value=None):
        return _ModuleBuilderCase(self, self._stmt_depth + 1, test, value)

    def _flush(self):
        if self._stmt_if_cond:
            tests, cases = [], OrderedDict()
            for if_cond, if_case in zip(self._stmt_if_cond + [None], self._stmt_if_bodies):
                if if_cond is not None:
                    if_cond = Value.wrap(if_cond)
                    if len(if_cond) != 1:
                        if_cond = if_cond.bool()
                    tests.append(if_cond)

                if if_cond is not None:
                    match = ("1" + "-" * (len(tests) - 1)).rjust(len(self._stmt_if_cond), "-")
                else:
                    match = "-" * len(tests)
                cases[match] = if_case
            self._statements.append(Switch(Cat(tests), cases))

        if self._stmt_switch_test:
            self._statements.append(Switch(self._stmt_switch_test.value, self._stmt_switch_cases))

        self._stmt_if_cond      = []
        self._stmt_if_bodies    = []
        self._stmt_switch_test  = None
        self._stmt_switch_cases = OrderedDict()

    def _add_statement(self, assigns, cd_name, depth, compat_mode=False):
        def cd_human_name(cd_name):
            if cd_name is None:
                return "comb"
            else:
                return cd_name

        if depth < self._stmt_depth:
            self._flush()
        self._stmt_depth = depth

        for assign in Statement.wrap(assigns):
            if not compat_mode and not isinstance(assign, Assign):
                raise SyntaxError(
                    "Only assignments may be appended to d.{}"
                    .format(cd_human_name(cd_name)))

            for signal in assign._lhs_signals():
                if signal not in self._driving:
                    self._driving[signal] = cd_name
                elif self._driving[signal] != cd_name:
                    cd_curr = self._driving[signal]
                    raise SyntaxError(
                        "Driver-driver conflict: trying to drive {!r} from d.{}, but it is "
                        "already driven from d.{}"
                        .format(signal, cd_human_name(cd_name), cd_human_name(cd_curr)))

            self._statements.append(assign)

    def _add_submodule(self, submodule, name=None):
        if not hasattr(submodule, "get_fragment"):
            raise TypeError("Trying to add {!r}, which does not implement .get_fragment(), as "
                            "a submodule".format(submodule))
        self._submodules.append((submodule, name))

    def lower(self, platform):
        self._flush()

        fragment = Fragment()
        for submodule, name in self._submodules:
            fragment.add_subfragment(submodule.get_fragment(platform), name)
        fragment.add_statements(self._statements)
        for signal, cd_name in self._driving.items():
            fragment.drive(signal, cd_name)
        return fragment

    get_fragment = lower
