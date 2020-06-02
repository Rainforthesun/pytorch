import os
import io
import sys
import random
import torch
from itertools import product as product
from torch import Tensor
from typing import NamedTuple

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from torch.testing._internal.jit_utils import (JitTestCase,
                                               clear_class_registry)

if __name__ == "__main__":
    raise RuntimeError(
        "This test file is not meant to be run directly, use:\n\n"
        "\tpython test/test_jit.py TESTNAME\n\n"
        "instead."
    )

class TestSaveLoad(JitTestCase):
    def test_versioned_symbols(self):
        """
        Tests Torchscript symbol versioning. See note [Versioned Symbols].
        This test uses an undocumented, test-only function
        torch._test_serialization_subcmul.

        This function is implemented as (a - alpha * b) with a default value
        of 1 for alpha. In file format version 2, however, it was implemented
        as (b - alpha * a) with a default value of 2 for alpha.
        This test verifies a module seralized with file format version 2
        exhibits the old behavior, and that the same module newly serialized
        exhibits the current behavior.
        """
        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, a, b, alpha: float):
                no_alpha = torch._test_serialization_subcmul(a, b)
                with_alpha = torch._test_serialization_subcmul(a, b, alpha)
                return no_alpha, with_alpha

        def historic_subcmul(a, b, alpha=2):
            return b - alpha * a

        def current_subcmul(a, b, alpha=1):
            return a - alpha * b

        # Loads and verifies the historic behavior of the module
        # that was serialized with version 2
        module_v2 = torch.jit.load(pytorch_test_dir + "/jit/fixtures/_test_serialization_subcmul_v2.pt")
        a = torch.randn((5,))
        b = torch.randn((5,))
        alpha = random.random()
        args = (a, b, alpha)
        no_alpha_v2, with_alpha_v2 = module_v2(*args)
        self.assertEqual(no_alpha_v2, historic_subcmul(a, b))
        self.assertEqual(with_alpha_v2, historic_subcmul(*args))

        # Scripts, saves, loads and verifies the current behavior of the module
        scripted_module = torch.jit.script(MyModule())
        buffer = io.BytesIO()
        torch.jit.save(scripted_module, buffer)
        buffer.seek(0)
        module_current = torch.jit.load(buffer)
        no_alpha_current, with_alpha_current = module_current(*args)
        self.assertEqual(no_alpha_current, current_subcmul(a, b))
        self.assertEqual(with_alpha_current, current_subcmul(*args))

    # Helper that returns the module after saving and loading
    def _save_load_module(self, m):
        scripted_module = torch.jit.script(m())
        buffer = io.BytesIO()
        torch.jit.save(scripted_module, buffer)
        buffer.seek(0)
        return torch.jit.load(buffer)

    # Helper which returns the result of a function or the exception the
    #   function threw.
    def _try_fn(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return e

    def _verify_no(self, kind, m):
        node_count = sum(kind in str(n) for n in m.graph.nodes())
        self.assertEqual(node_count, 0)

    def _verify_count(self, kind, m, count):
        node_count = sum(kind in str(n) for n in m.graph.nodes())
        self.assertEqual(node_count, count)

    """
    Tests that verify Torchscript remaps aten::div(_) from versions 0-3
    to call either aten::true_divide(_), if an input is a float type,
    or aten::floor_divide(_) otherwise.

    NOTE: currently compares against current div behavior, too, since
      div behavior has not yet been updated.
    """

    def test_versioned_div_tensor(self):
        def historic_div(self, other):
            if self.is_floating_point() or other.is_floating_point():
                return self.true_divide(other)
            return self.floor_divide(other)

        # Tensor x Tensor
        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, a, b):
                result_0 = a / b
                result_1 = torch.div(a, b)
                result_2 = a.div(b)

                return result_0, result_1, result_2

        # Loads historic module
        v3_module = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_tensor_v3.pt")
        self._verify_no("aten::div", v3_module)
        self._verify_count("aten::true_divide", v3_module, 3)
        self._verify_count("aten::floor_divide", v3_module, 3)

        current_module = self._save_load_module(MyModule)
        self._verify_count("aten::div", current_module, 3)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = torch.tensor((val_b,))

            def _helper(m, fn):
                m_results = self._try_fn(m, a, b)
                fn_result = self._try_fn(fn, a, b)

                if isinstance(m_results, Exception):
                    self.assertTrue(isinstance(fn_result, Exception))
                else:
                    for result in m_results:
                        self.assertEqual(result, fn_result)

            _helper(v3_module, historic_div)
            _helper(current_module, torch.div)

    def test_versioned_div_tensor_inplace(self):
        def historic_div_(self, other):
            if self.is_floating_point() or other.is_floating_point():
                return self.true_divide_(other)
            return self.floor_divide_(other)

        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, a, b):
                a /= b
                return a

        v3_module = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_tensor_inplace_v3.pt")
        self._verify_no("aten::div", v3_module)
        self._verify_count("aten::true_divide", v3_module, 1)
        self._verify_count("aten::floor_divide", v3_module, 1)

        current_module = self._save_load_module(MyModule)
        self._verify_count("aten::div", current_module, 1)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = torch.tensor((val_b,))

            def _helper(m, fn):
                fn_result = self._try_fn(fn, a.clone(), b)
                m_result = self._try_fn(m, a, b)

                if isinstance(m_result, Exception):
                    self.assertTrue(fn_result, Exception)
                else:
                    self.assertEqual(m_result, fn_result)
                    self.assertEqual(m_result, a)

            _helper(v3_module, historic_div_)

            # Recreates a since it was modified in place
            a = torch.tensor((val_a,))
            _helper(current_module, torch.Tensor.div_)

    def test_versioned_div_tensor_out(self):
        def historic_div_out(self, other, out):
            if self.is_floating_point() or other.is_floating_point() or out.is_floating_point():
                return torch.true_divide(self, other, out=out)
            return torch.floor_divide(self, other, out=out)

        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, a, b, out):
                return a.div(b, out=out)

        # scripted_module = self._save_load_module(MyModule)
        v3_module = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_tensor_out_v3.pt")
        self._verify_no("aten::div", v3_module)
        self._verify_count("aten::true_divide", v3_module, 1)
        self._verify_count("aten::floor_divide", v3_module, 1)

        current_module = self._save_load_module(MyModule)
        self._verify_count("aten::div", current_module, 1)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = torch.tensor((val_b,))

            for out in (torch.empty((1,)), torch.empty((1,), dtype=torch.long)):
                def _helper(m, fn):
                    fn_result = None
                    if fn is torch.div:
                        fn_result = self._try_fn(fn, a, b, out=out.clone())
                    else:
                        fn_result = self._try_fn(fn, a, b, out.clone())
                    m_result = self._try_fn(m, a, b, out)

                    if isinstance(m_result, Exception):
                        self.assertTrue(fn_result, Exception)
                    else:
                        self.assertEqual(m_result, fn_result)
                        self.assertEqual(m_result, out)

                _helper(v3_module, historic_div_out)
                _helper(current_module, torch.div)

    def test_versioned_div_scalar(self):
        def historic_div_scalar_float(self, other: float):
            return torch.true_divide(self, other)

        def historic_div_scalar_int(self, other: int):
            if self.is_floating_point():
                return torch.true_divide(self, other)
            return torch.floor_divide(self, other)

        class MyModuleFloat(torch.nn.Module):
            def __init__(self):
                super(MyModuleFloat, self).__init__()

            def forward(self, a, b: float):
                return a / b

        class MyModuleInt(torch.nn.Module):
            def __init__(self):
                super(MyModuleInt, self).__init__()

            def forward(self, a, b: int):
                return a / b

        v3_module_float = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_float_v3.pt")
        v3_module_int = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_int_v3.pt")

        for m in (v3_module_float, v3_module_int):
            self._verify_no("aten::div", m)
            self._verify_count("aten::true_divide", m, 1)
            self._verify_count("aten::floor_divide", m, 1)

        current_module_float = self._save_load_module(MyModuleFloat)
        current_module_int = self._save_load_module(MyModuleInt)

        for m in (current_module_float, current_module_int):
            self._verify_count("aten::div", m, 1)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = val_b

            def _helper(m, fn):
                m_result = self._try_fn(m, a, b)
                fn_result = self._try_fn(fn, a, b)

                if isinstance(m_result, Exception):
                    self.assertTrue(fn_result, Exception)
                else:
                    self.assertEqual(m_result, fn_result)

            if isinstance(b, float):
                _helper(v3_module_float, historic_div_scalar_float)
                _helper(current_module_float, torch.div)
            else:
                _helper(v3_module_int, historic_div_scalar_int)
                _helper(current_module_int, torch.div)

    def test_versioned_div_scalar_reciprocal(self):
        def historic_div_scalar_float_reciprocal(self, other: float):
            return other / self

        def historic_div_scalar_int_reciprocal(self, other: int):
            if self.is_floating_point():
                return other / self
            return other // self

        class MyModuleFloat(torch.nn.Module):
            def __init__(self):
                super(MyModuleFloat, self).__init__()

            def forward(self, a, b: float):
                return b / a

        class MyModuleInt(torch.nn.Module):
            def __init__(self):
                super(MyModuleInt, self).__init__()

            def forward(self, a, b: int):
                return b / a

        v3_module_float = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_reciprocal_float_v3.pt")
        v3_module_int = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_reciprocal_int_v3.pt")

        # NOTE: number / tensor is rewritten to torch.reciprocal(a) * b
        #  so true_divide and floor_divide do not appear in their graphs
        for m in (v3_module_float, v3_module_int):
            self._verify_no("aten::div", m)
            self._verify_no("aten::true_divide", m)
            self._verify_no("aten::floor_divide", m)
            self._verify_count("aten::reciprocal", m, 1)

        current_module_float = self._save_load_module(MyModuleFloat)
        current_module_int = self._save_load_module(MyModuleInt)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = val_b

            def _helper(m, fn):
                m_result = self._try_fn(m, a, b)
                fn_result = None
                # Reverses argument order for torch.div
                if fn is torch.div:
                    fn_result = self._try_fn(torch.div, b, a)
                else:
                    fn_result = self._try_fn(fn, a, b)

                if not a.is_floating_point():
                    # NOTE: Torchscript rewrites the module forward into
                    #   torch.reciprocal(a) * b, but torch.reciprocal is
                    #   implemented for integer dtypes.
                    self.assertTrue(m_result, Exception)
                    self.assertTrue('"reciprocal_cpu" not implemented for' in str(m_result))
                elif isinstance(m_result, Exception):
                    self.assertTrue(fn_result, Exception)
                else:
                    self.assertEqual(m_result, fn_result)

            if isinstance(b, float):
                _helper(v3_module_float, historic_div_scalar_float_reciprocal)
                _helper(current_module_float, torch.div)
            else:
                _helper(v3_module_int, historic_div_scalar_int_reciprocal)
                _helper(current_module_int, torch.div)

    def test_versioned_div_scalar_inplace(self):
        def historic_div_scalar_float_inplace(self, other: float):
            return self.true_divide_(other)

        def historic_div_scalar_int_inplace(self, other: int):
            if self.is_floating_point():
                return self.true_divide_(other)

            return self.floor_divide_(other)

        class MyModuleFloat(torch.nn.Module):
            def __init__(self):
                super(MyModuleFloat, self).__init__()

            def forward(self, a, b: float):
                a /= b
                return a

        class MyModuleInt(torch.nn.Module):
            def __init__(self):
                super(MyModuleInt, self).__init__()

            def forward(self, a, b: int):
                a /= b
                return a

        v3_module_float = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_inplace_float_v3.pt")
        v3_module_int = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_inplace_int_v3.pt")

        for m in (v3_module_float, v3_module_int):
            self._verify_no("aten::div", m)
            self._verify_count("aten::true_divide", m, 1)
            self._verify_count("aten::floor_divide", m, 1)

        current_module_float = self._save_load_module(MyModuleFloat)
        current_module_int = self._save_load_module(MyModuleInt)

        for m in (current_module_float, current_module_int):
            self._verify_count("aten::div", m, 1)

        for m in (current_module_float, current_module_int):
            self._verify_count("aten::div", m, 1)

        vals = (2., 3., 2, 3)
        for val_a, val_b in product(vals, vals):
            a = torch.tensor((val_a,))
            b = val_b

            def _helper(m, fn):
                m_result = self._try_fn(m, a, b)
                fn_result = self._try_fn(fn, a, b)

                if isinstance(m_result, Exception):
                    self.assertTrue(fn_result, Exception)
                else:
                    self.assertEqual(m_result, fn_result)

            if isinstance(b, float):
                _helper(v3_module_float, historic_div_scalar_float_inplace)
                _helper(current_module_float, torch.Tensor.div_)
            else:
                _helper(v3_module_int, historic_div_scalar_int_inplace)
                _helper(current_module_int, torch.Tensor.div_)

    # NOTE: Scalar division was already true division in op version 3,
    #   so this test verifies the behavior is unchanged.
    def test_versioned_div_scalar_scalar(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, a: float, b: int, c: float, d: int):
                result_0 = a / b
                result_1 = a / c
                result_2 = b / c
                result_3 = b / d
                return (result_0, result_1, result_2, result_3)

        v3_module = torch.jit.load(pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_scalar_v3.pt")
        self._verify_count("aten::div", v3_module, 4)

        current_module = self._save_load_module(MyModule)
        self._verify_count("aten::div", current_module, 4)

        def _helper(m, fn):
            vals = (5., 3, 2., 7)
            m_result = m(*vals)
            fn_result = fn(*vals)
            for mr, hr in zip(m_result, fn_result):
                self.assertEqual(mr, hr)

        _helper(v3_module, current_module)

    def test_versioned_symbols_reserialization(self):
        """
        Tests that loading and saving serialized Torchscript with a versioned
        symbol won't persist the original function and will inline the
        versioned builtin.
        """
        module_v2 = torch.jit.load(pytorch_test_dir + "/jit/fixtures/_test_serialization_subcmul_v2.pt")
        buffer = io.BytesIO()
        torch.jit.save(module_v2, buffer)
        buffer.seek(0)
        module_reserialized = torch.jit.load(buffer)

        subcmul_nodes = sum("subcmul" in n.kind() for
                            n in module_reserialized.graph.nodes())
        self.assertEqual(subcmul_nodes, 0)

    def test_different_modules(self):
        """
        Exercise the situation where we have the same qualified name
        in two different CompilationUnits on save/load.
        """
        class Foo(torch.nn.Module):
            def __init__(self):
                super(Foo, self).__init__()
                self.foo = torch.nn.Linear(2, 2)
                self.bar = torch.nn.Linear(2, 2)

            def forward(self, x):
                x = self.foo(x)
                x = self.bar(x)
                return x

        first_script_module = torch.jit.script(Foo())
        first_saved_module = io.BytesIO()
        torch.jit.save(first_script_module, first_saved_module)
        first_saved_module.seek(0)

        clear_class_registry()

        class Foo(torch.nn.Module):
            def __init__(self):
                super(Foo, self).__init__()
                self.foo = torch.nn.Linear(2, 2)

            def forward(self, x):
                x = self.foo(x)
                return x

        second_script_module = torch.jit.script(Foo())
        second_saved_module = io.BytesIO()
        torch.jit.save(torch.jit.script(Foo()), second_saved_module)
        second_saved_module.seek(0)

        clear_class_registry()

        self.assertEqual(
            first_script_module._c.qualified_name, second_script_module._c.qualified_name
        )

        class ContainsBoth(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.add_module("second", torch.jit.load(second_saved_module))
                self.add_module("first", torch.jit.load(first_saved_module))

            def forward(self, x):
                x = self.first(x)
                x = self.second(x)
                return x

        sm = torch.jit.script(ContainsBoth())
        contains_both = io.BytesIO()
        torch.jit.save(sm, contains_both)
        contains_both.seek(0)
        sm = torch.jit.load(contains_both)

    def test_different_functions(self):
        """
        Exercise the situation where we have the same qualified name
        in two different CompilationUnits on save/load.
        """
        def lol(x):
            return x

        class Foo(torch.nn.Module):
            def forward(self, x):
                return lol(x)

        first_script_module = torch.jit.script(Foo())
        first_saved_module = io.BytesIO()
        torch.jit.save(first_script_module, first_saved_module)
        first_saved_module.seek(0)

        clear_class_registry()

        def lol(x):  # noqa: F811
            return "hello"

        class Foo(torch.nn.Module):
            def forward(self, x):
                return lol(x)

        second_script_module = torch.jit.script(Foo())
        second_saved_module = io.BytesIO()
        torch.jit.save(torch.jit.script(Foo()), second_saved_module)
        second_saved_module.seek(0)

        clear_class_registry()

        self.assertEqual(
            first_script_module._c.qualified_name, second_script_module._c.qualified_name
        )

        class ContainsBoth(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.add_module("second", torch.jit.load(second_saved_module))
                self.add_module("first", torch.jit.load(first_saved_module))

            def forward(self, x):
                x = self.first(x)
                x = self.second(x)
                return x

        sm = torch.jit.script(ContainsBoth())
        contains_both = io.BytesIO()
        torch.jit.save(sm, contains_both)
        contains_both.seek(0)
        sm = torch.jit.load(contains_both)

    def test_different_interfaces(self):
        """
        Exercise the situation where we have the same qualified name
        in two different CompilationUnits on save/load.
        """
        @torch.jit.interface
        class MyInterface(object):
            def bar(self, x):
                # type: (Tensor) -> Tensor
                pass

        @torch.jit.script
        class ImplementInterface(object):
            def __init__(self):
                pass

            def bar(self, x):
                return x

        class Foo(torch.nn.Module):
            __annotations__ = {"interface": MyInterface}

            def __init__(self):
                super().__init__()
                self.interface = ImplementInterface()

            def forward(self, x):
                return self.interface.bar(x)

        first_script_module = torch.jit.script(Foo())
        first_saved_module = io.BytesIO()
        torch.jit.save(first_script_module, first_saved_module)
        first_saved_module.seek(0)

        clear_class_registry()

        @torch.jit.interface
        class MyInterface(object):
            def not_bar(self, x):
                # type: (Tensor) -> Tensor
                pass

        @torch.jit.script  # noqa: F811
        class ImplementInterface(object):  # noqa: F811
            def __init__(self):
                pass

            def not_bar(self, x):
                return x

        class Foo(torch.nn.Module):
            __annotations__ = {"interface": MyInterface}

            def __init__(self):
                super().__init__()
                self.interface = ImplementInterface()

            def forward(self, x):
                return self.interface.not_bar(x)

        second_script_module = torch.jit.script(Foo())
        second_saved_module = io.BytesIO()
        torch.jit.save(torch.jit.script(Foo()), second_saved_module)
        second_saved_module.seek(0)

        clear_class_registry()

        self.assertEqual(
            first_script_module._c.qualified_name, second_script_module._c.qualified_name
        )

        class ContainsBoth(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.add_module("second", torch.jit.load(second_saved_module))
                self.add_module("first", torch.jit.load(first_saved_module))

            def forward(self, x):
                x = self.first(x)
                x = self.second(x)
                return x

        sm = torch.jit.script(ContainsBoth())
        contains_both = io.BytesIO()
        torch.jit.save(sm, contains_both)
        contains_both.seek(0)
        sm = torch.jit.load(contains_both)

    def test_many_collisions(self):
        class MyCoolNamedTuple(NamedTuple):
            a: int

        @torch.jit.interface
        class MyInterface(object):
            def bar(self, x):
                # type: (Tensor) -> Tensor
                pass

        @torch.jit.script
        class ImplementInterface(object):
            def __init__(self):
                pass

            def bar(self, x):
                return x

        def lol(x):
            return x

        class Foo(torch.nn.Module):
            interface: MyInterface

            def __init__(self):
                super().__init__()
                self.foo = torch.nn.Linear(2, 2)
                self.bar = torch.nn.Linear(2, 2)
                self.interface = ImplementInterface()

            def forward(self, x):
                x = self.foo(x)
                x = self.bar(x)
                x = lol(x)
                x = self.interface.bar(x)

                return x, MyCoolNamedTuple(a=5)


        first_script_module = torch.jit.script(Foo())
        first_saved_module = io.BytesIO()
        torch.jit.save(first_script_module, first_saved_module)
        first_saved_module.seek(0)

        clear_class_registry()

        @torch.jit.interface
        class MyInterface(object):
            def not_bar(self, x):
                # type: (Tensor) -> Tensor
                pass

        @torch.jit.script   # noqa F811
        class ImplementInterface(object):  # noqa F811
            def __init__(self):
                pass

            def not_bar(self, x):
                return x

        def lol(x):  # noqa F811
            return "asdofij"

        class MyCoolNamedTuple(NamedTuple):  # noqa F811
            a: str

        class Foo(torch.nn.Module):
            interface: MyInterface

            def __init__(self):
                super().__init__()
                self.foo = torch.nn.Linear(2, 2)
                self.interface = ImplementInterface()

            def forward(self, x):
                x = self.foo(x)
                self.interface.not_bar(x)
                x = lol(x)
                return x, MyCoolNamedTuple(a="hello")

        second_script_module = torch.jit.script(Foo())
        second_saved_module = io.BytesIO()
        torch.jit.save(second_script_module, second_saved_module)
        second_saved_module.seek(0)

        clear_class_registry()

        self.assertEqual(
            first_script_module._c.qualified_name, second_script_module._c.qualified_name
        )

        class ContainsBoth(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.add_module("second", torch.jit.load(second_saved_module))
                self.add_module("first", torch.jit.load(first_saved_module))

            def forward(self, x):
                x, named_tuple_1 = self.first(x)
                x, named_tuple_2 = self.second(x)
                return len(x + named_tuple_2.a) + named_tuple_1.a

        sm = torch.jit.script(ContainsBoth())
        contains_both = io.BytesIO()
        torch.jit.save(sm, contains_both)
        contains_both.seek(0)
        sm = torch.jit.load(contains_both)
