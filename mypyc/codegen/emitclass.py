"""Code generation for native classes and related wrappers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from mypyc.codegen.emit import Emitter, HeaderDeclaration, ReturnHandler
from mypyc.codegen.emitfunc import native_function_header
from mypyc.codegen.emitwrapper import (
    generate_bin_op_wrapper,
    generate_bool_wrapper,
    generate_contains_wrapper,
    generate_dunder_wrapper,
    generate_get_wrapper,
    generate_hash_wrapper,
    generate_ipow_wrapper,
    generate_len_wrapper,
    generate_richcompare_wrapper,
    generate_set_del_item_wrapper,
)
from mypyc.common import BITMAP_BITS, BITMAP_TYPE, NATIVE_PREFIX, PREFIX, REG_PREFIX
from mypyc.ir.class_ir import ClassIR, VTableEntries
from mypyc.ir.func_ir import FUNC_CLASSMETHOD, FUNC_STATICMETHOD, FuncDecl, FuncIR
from mypyc.ir.rtypes import RTuple, RType, object_rprimitive
from mypyc.namegen import NameGenerator
from mypyc.sametype import is_same_type


def native_slot(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    return f"{NATIVE_PREFIX}{fn.cname(emitter.names)}"


# We maintain a table from dunder function names to struct slots they
# correspond to and functions that generate a wrapper (if necessary)
# and return the function name to stick in the slot.
# TODO: Add remaining dunder methods
SlotGenerator = Callable[[ClassIR, FuncIR, Emitter], str]
SlotTable = Mapping[str, tuple[str, SlotGenerator]]

SLOT_DEFS: SlotTable = {
    "__init__": ("tp_init", lambda c, t, e: generate_init_for_class(c, t, e)),
    "__call__": ("tp_call", lambda c, t, e: generate_call_wrapper(c, t, e)),
    "__str__": ("tp_str", native_slot),
    "__repr__": ("tp_repr", native_slot),
    "__next__": ("tp_iternext", native_slot),
    "__iter__": ("tp_iter", native_slot),
    "__hash__": ("tp_hash", generate_hash_wrapper),
    "__get__": ("tp_descr_get", generate_get_wrapper),
}

AS_MAPPING_SLOT_DEFS: SlotTable = {
    "__getitem__": ("mp_subscript", generate_dunder_wrapper),
    "__setitem__": ("mp_ass_subscript", generate_set_del_item_wrapper),
    "__delitem__": ("mp_ass_subscript", generate_set_del_item_wrapper),
    "__len__": ("mp_length", generate_len_wrapper),
}

AS_SEQUENCE_SLOT_DEFS: SlotTable = {"__contains__": ("sq_contains", generate_contains_wrapper)}

AS_NUMBER_SLOT_DEFS: SlotTable = {
    # Unary operations.
    "__bool__": ("nb_bool", generate_bool_wrapper),
    "__int__": ("nb_int", generate_dunder_wrapper),
    "__float__": ("nb_float", generate_dunder_wrapper),
    "__neg__": ("nb_negative", generate_dunder_wrapper),
    "__pos__": ("nb_positive", generate_dunder_wrapper),
    "__abs__": ("nb_absolute", generate_dunder_wrapper),
    "__invert__": ("nb_invert", generate_dunder_wrapper),
    # Binary operations.
    "__add__": ("nb_add", generate_bin_op_wrapper),
    "__radd__": ("nb_add", generate_bin_op_wrapper),
    "__sub__": ("nb_subtract", generate_bin_op_wrapper),
    "__rsub__": ("nb_subtract", generate_bin_op_wrapper),
    "__mul__": ("nb_multiply", generate_bin_op_wrapper),
    "__rmul__": ("nb_multiply", generate_bin_op_wrapper),
    "__mod__": ("nb_remainder", generate_bin_op_wrapper),
    "__rmod__": ("nb_remainder", generate_bin_op_wrapper),
    "__truediv__": ("nb_true_divide", generate_bin_op_wrapper),
    "__rtruediv__": ("nb_true_divide", generate_bin_op_wrapper),
    "__floordiv__": ("nb_floor_divide", generate_bin_op_wrapper),
    "__rfloordiv__": ("nb_floor_divide", generate_bin_op_wrapper),
    "__divmod__": ("nb_divmod", generate_bin_op_wrapper),
    "__rdivmod__": ("nb_divmod", generate_bin_op_wrapper),
    "__lshift__": ("nb_lshift", generate_bin_op_wrapper),
    "__rlshift__": ("nb_lshift", generate_bin_op_wrapper),
    "__rshift__": ("nb_rshift", generate_bin_op_wrapper),
    "__rrshift__": ("nb_rshift", generate_bin_op_wrapper),
    "__and__": ("nb_and", generate_bin_op_wrapper),
    "__rand__": ("nb_and", generate_bin_op_wrapper),
    "__or__": ("nb_or", generate_bin_op_wrapper),
    "__ror__": ("nb_or", generate_bin_op_wrapper),
    "__xor__": ("nb_xor", generate_bin_op_wrapper),
    "__rxor__": ("nb_xor", generate_bin_op_wrapper),
    "__matmul__": ("nb_matrix_multiply", generate_bin_op_wrapper),
    "__rmatmul__": ("nb_matrix_multiply", generate_bin_op_wrapper),
    # In-place binary operations.
    "__iadd__": ("nb_inplace_add", generate_dunder_wrapper),
    "__isub__": ("nb_inplace_subtract", generate_dunder_wrapper),
    "__imul__": ("nb_inplace_multiply", generate_dunder_wrapper),
    "__imod__": ("nb_inplace_remainder", generate_dunder_wrapper),
    "__itruediv__": ("nb_inplace_true_divide", generate_dunder_wrapper),
    "__ifloordiv__": ("nb_inplace_floor_divide", generate_dunder_wrapper),
    "__ilshift__": ("nb_inplace_lshift", generate_dunder_wrapper),
    "__irshift__": ("nb_inplace_rshift", generate_dunder_wrapper),
    "__iand__": ("nb_inplace_and", generate_dunder_wrapper),
    "__ior__": ("nb_inplace_or", generate_dunder_wrapper),
    "__ixor__": ("nb_inplace_xor", generate_dunder_wrapper),
    "__imatmul__": ("nb_inplace_matrix_multiply", generate_dunder_wrapper),
    # Ternary operations. (yes, really)
    # These are special cased in generate_bin_op_wrapper().
    "__pow__": ("nb_power", generate_bin_op_wrapper),
    "__rpow__": ("nb_power", generate_bin_op_wrapper),
    "__ipow__": ("nb_inplace_power", generate_ipow_wrapper),
}

AS_ASYNC_SLOT_DEFS: SlotTable = {
    "__await__": ("am_await", native_slot),
    "__aiter__": ("am_aiter", native_slot),
    "__anext__": ("am_anext", native_slot),
}

SIDE_TABLES = [
    ("as_mapping", "PyMappingMethods", AS_MAPPING_SLOT_DEFS),
    ("as_sequence", "PySequenceMethods", AS_SEQUENCE_SLOT_DEFS),
    ("as_number", "PyNumberMethods", AS_NUMBER_SLOT_DEFS),
    ("as_async", "PyAsyncMethods", AS_ASYNC_SLOT_DEFS),
]

# Slots that need to always be filled in because they don't get
# inherited right.
ALWAYS_FILL = {"__hash__"}


def generate_call_wrapper(cl: ClassIR, fn: FuncIR, emitter: Emitter) -> str:
    return "PyVectorcall_Call"


def slot_key(attr: str) -> str:
    """Map dunder method name to sort key.

    Sort reverse operator methods and __delitem__ after others ('x' > '_').
    """
    if (attr.startswith("__r") and attr != "__rshift__") or attr == "__delitem__":
        return "x" + attr
    return attr


def generate_slots(cl: ClassIR, table: SlotTable, emitter: Emitter) -> dict[str, str]:
    fields: dict[str, str] = {}
    generated: dict[str, str] = {}
    # Sort for determinism on Python 3.5
    for name, (slot, generator) in sorted(table.items(), key=lambda x: slot_key(x[0])):
        method_cls = cl.get_method_and_class(name)
        if method_cls and (method_cls[1] == cl or name in ALWAYS_FILL):
            if slot in generated:
                # Reuse previously generated wrapper.
                fields[slot] = generated[slot]
            else:
                # Generate new wrapper.
                name = generator(cl, method_cls[0], emitter)
                fields[slot] = name
                generated[slot] = name

    return fields


def generate_class_type_decl(
    cl: ClassIR, c_emitter: Emitter, external_emitter: Emitter, emitter: Emitter
) -> None:
    context = c_emitter.context
    name = emitter.type_struct_name(cl)
    context.declarations[name] = HeaderDeclaration(
        f"PyTypeObject *{emitter.type_struct_name(cl)};", needs_export=True
    )

    # If this is a non-extension class, all we want is the type object decl.
    if not cl.is_ext_class:
        return

    generate_object_struct(cl, external_emitter)
    generate_full = not cl.is_trait and not cl.builtin_base
    if generate_full:
        context.declarations[emitter.native_function_name(cl.ctor)] = HeaderDeclaration(
            f"{native_function_header(cl.ctor, emitter)};", needs_export=True
        )


def generate_class(cl: ClassIR, module: str, emitter: Emitter) -> None:
    """Generate C code for a class.

    This is the main entry point to the module.
    """
    name = cl.name
    name_prefix = cl.name_prefix(emitter.names)

    setup_name = f"{name_prefix}_setup"
    new_name = f"{name_prefix}_new"
    finalize_name = f"{name_prefix}_finalize"
    members_name = f"{name_prefix}_members"
    getseters_name = f"{name_prefix}_getseters"
    vtable_name = f"{name_prefix}_vtable"
    traverse_name = f"{name_prefix}_traverse"
    clear_name = f"{name_prefix}_clear"
    dealloc_name = f"{name_prefix}_dealloc"
    methods_name = f"{name_prefix}_methods"
    vtable_setup_name = f"{name_prefix}_trait_vtable_setup"

    fields: dict[str, str] = {"tp_name": f'"{name}"'}

    generate_full = not cl.is_trait and not cl.builtin_base
    needs_getseters = cl.needs_getseters or not cl.is_generated or cl.has_dict

    if not cl.builtin_base:
        fields["tp_new"] = new_name

    if generate_full:
        fields["tp_dealloc"] = f"(destructor){name_prefix}_dealloc"
        fields["tp_traverse"] = f"(traverseproc){name_prefix}_traverse"
        fields["tp_clear"] = f"(inquiry){name_prefix}_clear"
    # Populate .tp_finalize and generate a finalize method only if __del__ is defined for this class.
    del_method = next((e.method for e in cl.vtable_entries if e.name == "__del__"), None)
    if del_method:
        fields["tp_finalize"] = f"(destructor){finalize_name}"
    if needs_getseters:
        fields["tp_getset"] = getseters_name
    fields["tp_methods"] = methods_name

    def emit_line() -> None:
        emitter.emit_line()

    emit_line()

    # If the class has a method to initialize default attribute
    # values, we need to call it during initialization.
    defaults_fn = cl.get_method("__mypyc_defaults_setup")

    # If there is a __init__ method, we'll use it in the native constructor.
    init_fn = cl.get_method("__init__")

    # Fill out slots in the type object from dunder methods.
    fields.update(generate_slots(cl, SLOT_DEFS, emitter))

    # Fill out dunder methods that live in tables hanging off the side.
    for table_name, type, slot_defs in SIDE_TABLES:
        slots = generate_slots(cl, slot_defs, emitter)
        if slots:
            table_struct_name = generate_side_table_for_class(cl, table_name, type, slots, emitter)
            fields[f"tp_{table_name}"] = f"&{table_struct_name}"

    richcompare_name = generate_richcompare_wrapper(cl, emitter)
    if richcompare_name:
        fields["tp_richcompare"] = richcompare_name

    # If the class inherits from python, make space for a __dict__
    struct_name = cl.struct_name(emitter.names)
    if cl.builtin_base:
        base_size = f"sizeof({cl.builtin_base})"
    elif cl.is_trait:
        base_size = "sizeof(PyObject)"
    else:
        base_size = f"sizeof({struct_name})"
    # Since our types aren't allocated using type() we need to
    # populate these fields ourselves if we want them to have correct
    # values. PyType_Ready will inherit the offsets from tp_base but
    # that isn't what we want.

    # XXX: there is no reason for the __weakref__ stuff to be mixed up with __dict__
    if cl.has_dict and not has_managed_dict(cl, emitter):
        # __dict__ lives right after the struct and __weakref__ lives right after that
        # TODO: They should get members in the struct instead of doing this nonsense.
        weak_offset = f"{base_size} + sizeof(PyObject *)"
        emitter.emit_lines(
            f"PyMemberDef {members_name}[] = {{",
            f'{{"__dict__", T_OBJECT_EX, {base_size}, 0, NULL}},',
            f'{{"__weakref__", T_OBJECT_EX, {weak_offset}, 0, NULL}},',
            "{0}",
            "};",
        )

        fields["tp_members"] = members_name
        fields["tp_basicsize"] = f"{base_size} + 2*sizeof(PyObject *)"
        if emitter.capi_version < (3, 12):
            fields["tp_dictoffset"] = base_size
            fields["tp_weaklistoffset"] = weak_offset
    else:
        fields["tp_basicsize"] = base_size

    if generate_full:
        # Declare setup method that allocates and initializes an object. type is the
        # type of the class being initialized, which could be another class if there
        # is an interpreted subclass.
        emitter.emit_line(f"static PyObject *{setup_name}(PyTypeObject *type);")
        assert cl.ctor is not None
        emitter.emit_line(native_function_header(cl.ctor, emitter) + ";")

        emit_line()
        init_fn = cl.get_method("__init__")
        generate_new_for_class(cl, new_name, vtable_name, setup_name, init_fn, emitter)
        emit_line()
        generate_traverse_for_class(cl, traverse_name, emitter)
        emit_line()
        generate_clear_for_class(cl, clear_name, emitter)
        emit_line()
        generate_dealloc_for_class(cl, dealloc_name, clear_name, bool(del_method), emitter)
        emit_line()

        if cl.allow_interpreted_subclasses:
            shadow_vtable_name: str | None = generate_vtables(
                cl, vtable_setup_name + "_shadow", vtable_name + "_shadow", emitter, shadow=True
            )
            emit_line()
        else:
            shadow_vtable_name = None
        vtable_name = generate_vtables(cl, vtable_setup_name, vtable_name, emitter, shadow=False)
        emit_line()
    if del_method:
        generate_finalize_for_class(del_method, finalize_name, emitter)
        emit_line()
    if needs_getseters:
        generate_getseter_declarations(cl, emitter)
        emit_line()
        generate_getseters_table(cl, getseters_name, emitter)
        emit_line()

    if cl.is_trait:
        generate_new_for_trait(cl, new_name, emitter)

    generate_methods_table(cl, methods_name, emitter)
    emit_line()

    flags = ["Py_TPFLAGS_DEFAULT", "Py_TPFLAGS_HEAPTYPE", "Py_TPFLAGS_BASETYPE"]
    if generate_full:
        flags.append("Py_TPFLAGS_HAVE_GC")
    if cl.has_method("__call__"):
        fields["tp_vectorcall_offset"] = "offsetof({}, vectorcall)".format(
            cl.struct_name(emitter.names)
        )
        flags.append("_Py_TPFLAGS_HAVE_VECTORCALL")
        if not fields.get("tp_vectorcall"):
            # This is just a placeholder to please CPython. It will be
            # overridden during setup.
            fields["tp_call"] = "PyVectorcall_Call"
    if has_managed_dict(cl, emitter):
        flags.append("Py_TPFLAGS_MANAGED_DICT")
    fields["tp_flags"] = " | ".join(flags)

    emitter.emit_line(f"static PyTypeObject {emitter.type_struct_name(cl)}_template_ = {{")
    emitter.emit_line("PyVarObject_HEAD_INIT(NULL, 0)")
    for field, value in fields.items():
        emitter.emit_line(f".{field} = {value},")
    emitter.emit_line("};")
    emitter.emit_line(
        "static PyTypeObject *{t}_template = &{t}_template_;".format(
            t=emitter.type_struct_name(cl)
        )
    )

    emitter.emit_line()
    if generate_full:
        generate_setup_for_class(
            cl, setup_name, defaults_fn, vtable_name, shadow_vtable_name, emitter
        )
        emitter.emit_line()
        generate_constructor_for_class(cl, cl.ctor, init_fn, setup_name, vtable_name, emitter)
        emitter.emit_line()
    if needs_getseters:
        generate_getseters(cl, emitter)


def getter_name(cl: ClassIR, attribute: str, names: NameGenerator) -> str:
    return names.private_name(cl.module_name, f"{cl.name}_get_{attribute}")


def setter_name(cl: ClassIR, attribute: str, names: NameGenerator) -> str:
    return names.private_name(cl.module_name, f"{cl.name}_set_{attribute}")


def generate_object_struct(cl: ClassIR, emitter: Emitter) -> None:
    seen_attrs: set[tuple[str, RType]] = set()
    lines: list[str] = []
    lines += ["typedef struct {", "PyObject_HEAD", "CPyVTableItem *vtable;"]
    if cl.has_method("__call__"):
        lines.append("vectorcallfunc vectorcall;")
    bitmap_attrs = []
    for base in reversed(cl.base_mro):
        if not base.is_trait:
            if base.bitmap_attrs:
                # Do we need another attribute bitmap field?
                if emitter.bitmap_field(len(base.bitmap_attrs) - 1) not in bitmap_attrs:
                    for i in range(0, len(base.bitmap_attrs), BITMAP_BITS):
                        attr = emitter.bitmap_field(i)
                        if attr not in bitmap_attrs:
                            lines.append(f"{BITMAP_TYPE} {attr};")
                            bitmap_attrs.append(attr)
            for attr, rtype in base.attributes.items():
                if (attr, rtype) not in seen_attrs:
                    lines.append(f"{emitter.ctype_spaced(rtype)}{emitter.attr(attr)};")
                    seen_attrs.add((attr, rtype))

                    if isinstance(rtype, RTuple):
                        emitter.declare_tuple_struct(rtype)

    lines.append(f"}} {cl.struct_name(emitter.names)};")
    lines.append("")
    emitter.context.declarations[cl.struct_name(emitter.names)] = HeaderDeclaration(
        lines, is_type=True
    )


def generate_vtables(
    base: ClassIR, vtable_setup_name: str, vtable_name: str, emitter: Emitter, shadow: bool
) -> str:
    """Emit the vtables and vtable setup functions for a class.

    This includes both the primary vtable and any trait implementation vtables.
    The trait vtables go before the main vtable, and have the following layout:
        {
            CPyType_T1,         // pointer to type object
            C_T1_trait_vtable,  // pointer to array of method pointers
            C_T1_offset_table,  // pointer to array of attribute offsets
            CPyType_T2,
            C_T2_trait_vtable,
            C_T2_offset_table,
            ...
        }
    The method implementations are calculated at the end of IR pass, attribute
    offsets are {offsetof(native__C, _x1), offsetof(native__C, _y1), ...}.

    To account for both dynamic loading and dynamic class creation,
    vtables are populated dynamically at class creation time, so we
    emit empty array definitions to store the vtables and a function to
    populate them.

    If shadow is True, generate "shadow vtables" that point to the
    shadow glue methods (which should dispatch via the Python C-API).

    Returns the expression to use to refer to the vtable, which might be
    different than the name, if there are trait vtables.
    """

    def trait_vtable_name(trait: ClassIR) -> str:
        return "{}_{}_trait_vtable{}".format(
            base.name_prefix(emitter.names),
            trait.name_prefix(emitter.names),
            "_shadow" if shadow else "",
        )

    def trait_offset_table_name(trait: ClassIR) -> str:
        return "{}_{}_offset_table".format(
            base.name_prefix(emitter.names), trait.name_prefix(emitter.names)
        )

    # Emit array definitions with enough space for all the entries
    emitter.emit_line(
        "static CPyVTableItem {}[{}];".format(
            vtable_name, max(1, len(base.vtable_entries) + 3 * len(base.trait_vtables))
        )
    )

    for trait, vtable in base.trait_vtables.items():
        # Trait methods entry (vtable index -> method implementation).
        emitter.emit_line(
            f"static CPyVTableItem {trait_vtable_name(trait)}[{max(1, len(vtable))}];"
        )
        # Trait attributes entry (attribute number in trait -> offset in actual struct).
        emitter.emit_line(
            "static size_t {}[{}];".format(
                trait_offset_table_name(trait), max(1, len(trait.attributes))
            )
        )

    # Emit vtable setup function
    emitter.emit_line("static bool")
    emitter.emit_line(f"{NATIVE_PREFIX}{vtable_setup_name}(void)")
    emitter.emit_line("{")

    if base.allow_interpreted_subclasses and not shadow:
        emitter.emit_line(f"{NATIVE_PREFIX}{vtable_setup_name}_shadow();")

    subtables = []
    for trait, vtable in base.trait_vtables.items():
        name = trait_vtable_name(trait)
        offset_name = trait_offset_table_name(trait)
        generate_vtable(vtable, name, emitter, [], shadow)
        generate_offset_table(offset_name, emitter, trait, base)
        subtables.append((trait, name, offset_name))

    generate_vtable(base.vtable_entries, vtable_name, emitter, subtables, shadow)

    emitter.emit_line("return 1;")
    emitter.emit_line("}")

    return vtable_name if not subtables else f"{vtable_name} + {len(subtables) * 3}"


def generate_offset_table(
    trait_offset_table_name: str, emitter: Emitter, trait: ClassIR, cl: ClassIR
) -> None:
    """Generate attribute offset row of a trait vtable."""
    emitter.emit_line(f"size_t {trait_offset_table_name}_scratch[] = {{")
    for attr in trait.attributes:
        emitter.emit_line(f"offsetof({cl.struct_name(emitter.names)}, {emitter.attr(attr)}),")
    if not trait.attributes:
        # This is for msvc.
        emitter.emit_line("0")
    emitter.emit_line("};")
    emitter.emit_line(
        "memcpy({name}, {name}_scratch, sizeof({name}));".format(name=trait_offset_table_name)
    )


def generate_vtable(
    entries: VTableEntries,
    vtable_name: str,
    emitter: Emitter,
    subtables: list[tuple[ClassIR, str, str]],
    shadow: bool,
) -> None:
    emitter.emit_line(f"CPyVTableItem {vtable_name}_scratch[] = {{")
    if subtables:
        emitter.emit_line("/* Array of trait vtables */")
        for trait, table, offset_table in subtables:
            emitter.emit_line(
                "(CPyVTableItem){}, (CPyVTableItem){}, (CPyVTableItem){},".format(
                    emitter.type_struct_name(trait), table, offset_table
                )
            )
        emitter.emit_line("/* Start of real vtable */")

    for entry in entries:
        method = entry.shadow_method if shadow and entry.shadow_method else entry.method
        emitter.emit_line(
            "(CPyVTableItem){}{}{},".format(
                emitter.get_group_prefix(entry.method.decl),
                NATIVE_PREFIX,
                method.cname(emitter.names),
            )
        )

    # msvc doesn't allow empty arrays; maybe allowing them at all is an extension?
    if not entries:
        emitter.emit_line("NULL")
    emitter.emit_line("};")
    emitter.emit_line("memcpy({name}, {name}_scratch, sizeof({name}));".format(name=vtable_name))


def generate_setup_for_class(
    cl: ClassIR,
    func_name: str,
    defaults_fn: FuncIR | None,
    vtable_name: str,
    shadow_vtable_name: str | None,
    emitter: Emitter,
) -> None:
    """Generate a native function that allocates an instance of a class."""
    emitter.emit_line("static PyObject *")
    emitter.emit_line(f"{func_name}(PyTypeObject *type)")
    emitter.emit_line("{")
    emitter.emit_line(f"{cl.struct_name(emitter.names)} *self;")
    emitter.emit_line(f"self = ({cl.struct_name(emitter.names)} *)type->tp_alloc(type, 0);")
    emitter.emit_line("if (self == NULL)")
    emitter.emit_line("    return NULL;")

    if shadow_vtable_name:
        emitter.emit_line(f"if (type != {emitter.type_struct_name(cl)}) {{")
        emitter.emit_line(f"self->vtable = {shadow_vtable_name};")
        emitter.emit_line("} else {")
        emitter.emit_line(f"self->vtable = {vtable_name};")
        emitter.emit_line("}")
    else:
        emitter.emit_line(f"self->vtable = {vtable_name};")

    for i in range(0, len(cl.bitmap_attrs), BITMAP_BITS):
        field = emitter.bitmap_field(i)
        emitter.emit_line(f"self->{field} = 0;")

    if cl.has_method("__call__"):
        name = cl.method_decl("__call__").cname(emitter.names)
        emitter.emit_line(f"self->vectorcall = {PREFIX}{name};")

    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            value = emitter.c_undefined_value(rtype)

            # We don't need to set this field to NULL since tp_alloc() already
            # zero-initializes `self`.
            if value != "NULL":
                emitter.emit_line(rf"self->{emitter.attr(attr)} = {value};")

    # Initialize attributes to default values, if necessary
    if defaults_fn is not None:
        emitter.emit_lines(
            "if ({}{}((PyObject *)self) == 0) {{".format(
                NATIVE_PREFIX, defaults_fn.cname(emitter.names)
            ),
            "Py_DECREF(self);",
            "return NULL;",
            "}",
        )

    emitter.emit_line("return (PyObject *)self;")
    emitter.emit_line("}")


def generate_constructor_for_class(
    cl: ClassIR,
    fn: FuncDecl,
    init_fn: FuncIR | None,
    setup_name: str,
    vtable_name: str,
    emitter: Emitter,
) -> None:
    """Generate a native function that allocates and initializes an instance of a class."""
    emitter.emit_line(f"{native_function_header(fn, emitter)}")
    emitter.emit_line("{")
    emitter.emit_line(f"PyObject *self = {setup_name}({emitter.type_struct_name(cl)});")
    emitter.emit_line("if (self == NULL)")
    emitter.emit_line("    return NULL;")
    args = ", ".join(["self"] + [REG_PREFIX + arg.name for arg in fn.sig.args])
    if init_fn is not None:
        emitter.emit_line(
            "char res = {}{}{}({});".format(
                emitter.get_group_prefix(init_fn.decl),
                NATIVE_PREFIX,
                init_fn.cname(emitter.names),
                args,
            )
        )
        emitter.emit_line("if (res == 2) {")
        emitter.emit_line("Py_DECREF(self);")
        emitter.emit_line("return NULL;")
        emitter.emit_line("}")

    # If there is a nontrivial ctor that we didn't define, invoke it via tp_init
    elif len(fn.sig.args) > 1:
        emitter.emit_line(f"int res = {emitter.type_struct_name(cl)}->tp_init({args});")

        emitter.emit_line("if (res < 0) {")
        emitter.emit_line("Py_DECREF(self);")
        emitter.emit_line("return NULL;")
        emitter.emit_line("}")

    emitter.emit_line("return self;")
    emitter.emit_line("}")


def generate_init_for_class(cl: ClassIR, init_fn: FuncIR, emitter: Emitter) -> str:
    """Generate an init function suitable for use as tp_init.

    tp_init needs to be a function that returns an int, and our
    __init__ methods return a PyObject. Translate NULL to -1,
    everything else to 0.
    """
    func_name = f"{cl.name_prefix(emitter.names)}_init"

    emitter.emit_line("static int")
    emitter.emit_line(f"{func_name}(PyObject *self, PyObject *args, PyObject *kwds)")
    emitter.emit_line("{")
    if cl.allow_interpreted_subclasses or cl.builtin_base:
        emitter.emit_line(
            "return {}{}(self, args, kwds) != NULL ? 0 : -1;".format(
                PREFIX, init_fn.cname(emitter.names)
            )
        )
    else:
        emitter.emit_line("return 0;")
    emitter.emit_line("}")

    return func_name


def generate_new_for_class(
    cl: ClassIR,
    func_name: str,
    vtable_name: str,
    setup_name: str,
    init_fn: FuncIR | None,
    emitter: Emitter,
) -> None:
    emitter.emit_line("static PyObject *")
    emitter.emit_line(f"{func_name}(PyTypeObject *type, PyObject *args, PyObject *kwds)")
    emitter.emit_line("{")
    # TODO: Check and unbox arguments
    if not cl.allow_interpreted_subclasses:
        emitter.emit_line(f"if (type != {emitter.type_struct_name(cl)}) {{")
        emitter.emit_line(
            'PyErr_SetString(PyExc_TypeError, "interpreted classes cannot inherit from compiled");'
        )
        emitter.emit_line("return NULL;")
        emitter.emit_line("}")

    if not init_fn or cl.allow_interpreted_subclasses or cl.builtin_base or cl.is_serializable():
        # Match Python semantics -- __new__ doesn't call __init__.
        emitter.emit_line(f"return {setup_name}(type);")
    else:
        # __new__ of a native class implicitly calls __init__ so that we
        # can enforce that instances are always properly initialized. This
        # is needed to support always defined attributes.
        emitter.emit_line(f"PyObject *self = {setup_name}(type);")
        emitter.emit_lines("if (self == NULL)", "    return NULL;")
        emitter.emit_line(
            f"PyObject *ret = {PREFIX}{init_fn.cname(emitter.names)}(self, args, kwds);"
        )
        emitter.emit_lines("if (ret == NULL)", "    return NULL;")
        emitter.emit_line("return self;")
    emitter.emit_line("}")


def generate_new_for_trait(cl: ClassIR, func_name: str, emitter: Emitter) -> None:
    emitter.emit_line("static PyObject *")
    emitter.emit_line(f"{func_name}(PyTypeObject *type, PyObject *args, PyObject *kwds)")
    emitter.emit_line("{")
    emitter.emit_line(f"if (type != {emitter.type_struct_name(cl)}) {{")
    emitter.emit_line(
        "PyErr_SetString(PyExc_TypeError, "
        '"interpreted classes cannot inherit from compiled traits");'
    )
    emitter.emit_line("} else {")
    emitter.emit_line('PyErr_SetString(PyExc_TypeError, "traits may not be directly created");')
    emitter.emit_line("}")
    emitter.emit_line("return NULL;")
    emitter.emit_line("}")


def generate_traverse_for_class(cl: ClassIR, func_name: str, emitter: Emitter) -> None:
    """Emit function that performs cycle GC traversal of an instance."""
    emitter.emit_line("static int")
    emitter.emit_line(
        f"{func_name}({cl.struct_name(emitter.names)} *self, visitproc visit, void *arg)"
    )
    emitter.emit_line("{")
    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            emitter.emit_gc_visit(f"self->{emitter.attr(attr)}", rtype)
    if has_managed_dict(cl, emitter):
        emitter.emit_line("PyObject_VisitManagedDict((PyObject *)self, visit, arg);")
    elif cl.has_dict:
        struct_name = cl.struct_name(emitter.names)
        # __dict__ lives right after the struct and __weakref__ lives right after that
        emitter.emit_gc_visit(
            f"*((PyObject **)((char *)self + sizeof({struct_name})))", object_rprimitive
        )
        emitter.emit_gc_visit(
            f"*((PyObject **)((char *)self + sizeof(PyObject *) + sizeof({struct_name})))",
            object_rprimitive,
        )
    emitter.emit_line("return 0;")
    emitter.emit_line("}")


def generate_clear_for_class(cl: ClassIR, func_name: str, emitter: Emitter) -> None:
    emitter.emit_line("static int")
    emitter.emit_line(f"{func_name}({cl.struct_name(emitter.names)} *self)")
    emitter.emit_line("{")
    for base in reversed(cl.base_mro):
        for attr, rtype in base.attributes.items():
            emitter.emit_gc_clear(f"self->{emitter.attr(attr)}", rtype)
    if has_managed_dict(cl, emitter):
        emitter.emit_line("PyObject_ClearManagedDict((PyObject *)self);")
    elif cl.has_dict:
        struct_name = cl.struct_name(emitter.names)
        # __dict__ lives right after the struct and __weakref__ lives right after that
        emitter.emit_gc_clear(
            f"*((PyObject **)((char *)self + sizeof({struct_name})))", object_rprimitive
        )
        emitter.emit_gc_clear(
            f"*((PyObject **)((char *)self + sizeof(PyObject *) + sizeof({struct_name})))",
            object_rprimitive,
        )
    emitter.emit_line("return 0;")
    emitter.emit_line("}")


def generate_dealloc_for_class(
    cl: ClassIR,
    dealloc_func_name: str,
    clear_func_name: str,
    has_tp_finalize: bool,
    emitter: Emitter,
) -> None:
    emitter.emit_line("static void")
    emitter.emit_line(f"{dealloc_func_name}({cl.struct_name(emitter.names)} *self)")
    emitter.emit_line("{")
    if has_tp_finalize:
        emitter.emit_line("if (!PyObject_GC_IsFinalized((PyObject *)self)) {")
        emitter.emit_line("Py_TYPE(self)->tp_finalize((PyObject *)self);")
        emitter.emit_line("}")
    emitter.emit_line("PyObject_GC_UnTrack(self);")
    # The trashcan is needed to handle deep recursive deallocations
    emitter.emit_line(f"CPy_TRASHCAN_BEGIN(self, {dealloc_func_name})")
    emitter.emit_line(f"{clear_func_name}(self);")
    emitter.emit_line("Py_TYPE(self)->tp_free((PyObject *)self);")
    emitter.emit_line("CPy_TRASHCAN_END(self)")
    emitter.emit_line("}")


def generate_finalize_for_class(
    del_method: FuncIR, finalize_func_name: str, emitter: Emitter
) -> None:
    emitter.emit_line("static void")
    emitter.emit_line(f"{finalize_func_name}(PyObject *self)")
    emitter.emit_line("{")
    emitter.emit_line("PyObject *type, *value, *traceback;")
    emitter.emit_line("PyErr_Fetch(&type, &value, &traceback);")
    emitter.emit_line(
        "{}{}{}(self);".format(
            emitter.get_group_prefix(del_method.decl),
            NATIVE_PREFIX,
            del_method.cname(emitter.names),
        )
    )
    emitter.emit_line("if (PyErr_Occurred() != NULL) {")
    emitter.emit_line('PyObject *del_str = PyUnicode_FromString("__del__");')
    emitter.emit_line(
        "PyObject *del_method = (del_str == NULL) ? NULL : _PyType_Lookup(Py_TYPE(self), del_str);"
    )
    # CPython interpreter uses PyErr_WriteUnraisable: https://docs.python.org/3/c-api/exceptions.html#c.PyErr_WriteUnraisable
    # However, the message is slightly different due to the way mypyc compiles classes.
    # CPython interpreter prints: Exception ignored in: <function F.__del__ at 0x100aed940>
    # mypyc prints: Exception ignored in: <slot wrapper '__del__' of 'F' objects>
    emitter.emit_line("PyErr_WriteUnraisable(del_method);")
    emitter.emit_line("Py_XDECREF(del_method);")
    emitter.emit_line("Py_XDECREF(del_str);")
    emitter.emit_line("}")
    # PyErr_Restore also clears exception raised in __del__.
    emitter.emit_line("PyErr_Restore(type, value, traceback);")
    emitter.emit_line("}")


def generate_methods_table(cl: ClassIR, name: str, emitter: Emitter) -> None:
    emitter.emit_line(f"static PyMethodDef {name}[] = {{")
    for fn in cl.methods.values():
        if fn.decl.is_prop_setter or fn.decl.is_prop_getter or fn.internal:
            continue
        emitter.emit_line(f'{{"{fn.name}",')
        emitter.emit_line(f" (PyCFunction){PREFIX}{fn.cname(emitter.names)},")
        flags = ["METH_FASTCALL", "METH_KEYWORDS"]
        if fn.decl.kind == FUNC_STATICMETHOD:
            flags.append("METH_STATIC")
        elif fn.decl.kind == FUNC_CLASSMETHOD:
            flags.append("METH_CLASS")

        emitter.emit_line(" {}, NULL}},".format(" | ".join(flags)))

    # Provide a default __getstate__ and __setstate__
    if not cl.has_method("__setstate__") and not cl.has_method("__getstate__"):
        emitter.emit_lines(
            '{"__setstate__", (PyCFunction)CPyPickle_SetState, METH_O, NULL},',
            '{"__getstate__", (PyCFunction)CPyPickle_GetState, METH_NOARGS, NULL},',
        )

    emitter.emit_line("{NULL}  /* Sentinel */")
    emitter.emit_line("};")


def generate_side_table_for_class(
    cl: ClassIR, name: str, type: str, slots: dict[str, str], emitter: Emitter
) -> str | None:
    name = f"{cl.name_prefix(emitter.names)}_{name}"
    emitter.emit_line(f"static {type} {name} = {{")
    for field, value in slots.items():
        emitter.emit_line(f".{field} = {value},")
    emitter.emit_line("};")
    return name


def generate_getseter_declarations(cl: ClassIR, emitter: Emitter) -> None:
    if not cl.is_trait:
        for attr in cl.attributes:
            emitter.emit_line("static PyObject *")
            emitter.emit_line(
                "{}({} *self, void *closure);".format(
                    getter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
                )
            )
            emitter.emit_line("static int")
            emitter.emit_line(
                "{}({} *self, PyObject *value, void *closure);".format(
                    setter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
                )
            )

    for prop, (getter, setter) in cl.properties.items():
        if getter.decl.implicit:
            continue

        # Generate getter declaration
        emitter.emit_line("static PyObject *")
        emitter.emit_line(
            "{}({} *self, void *closure);".format(
                getter_name(cl, prop, emitter.names), cl.struct_name(emitter.names)
            )
        )

        # Generate property setter declaration if a setter exists
        if setter:
            emitter.emit_line("static int")
            emitter.emit_line(
                "{}({} *self, PyObject *value, void *closure);".format(
                    setter_name(cl, prop, emitter.names), cl.struct_name(emitter.names)
                )
            )


def generate_getseters_table(cl: ClassIR, name: str, emitter: Emitter) -> None:
    emitter.emit_line(f"static PyGetSetDef {name}[] = {{")
    if not cl.is_trait:
        for attr in cl.attributes:
            emitter.emit_line(f'{{"{attr}",')
            emitter.emit_line(
                " (getter){}, (setter){},".format(
                    getter_name(cl, attr, emitter.names), setter_name(cl, attr, emitter.names)
                )
            )
            emitter.emit_line(" NULL, NULL},")
    for prop, (getter, setter) in cl.properties.items():
        if getter.decl.implicit:
            continue

        emitter.emit_line(f'{{"{prop}",')
        emitter.emit_line(f" (getter){getter_name(cl, prop, emitter.names)},")

        if setter:
            emitter.emit_line(f" (setter){setter_name(cl, prop, emitter.names)},")
            emitter.emit_line("NULL, NULL},")
        else:
            emitter.emit_line("NULL, NULL, NULL},")

    if cl.has_dict:
        emitter.emit_line('{"__dict__", PyObject_GenericGetDict, PyObject_GenericSetDict},')

    emitter.emit_line("{NULL}  /* Sentinel */")
    emitter.emit_line("};")


def generate_getseters(cl: ClassIR, emitter: Emitter) -> None:
    if not cl.is_trait:
        for i, (attr, rtype) in enumerate(cl.attributes.items()):
            generate_getter(cl, attr, rtype, emitter)
            emitter.emit_line("")
            generate_setter(cl, attr, rtype, emitter)
            if i < len(cl.attributes) - 1:
                emitter.emit_line("")
    for prop, (getter, setter) in cl.properties.items():
        if getter.decl.implicit:
            continue

        rtype = getter.sig.ret_type
        emitter.emit_line("")
        generate_readonly_getter(cl, prop, rtype, getter, emitter)
        if setter:
            arg_type = setter.sig.args[1].type
            emitter.emit_line("")
            generate_property_setter(cl, prop, arg_type, setter, emitter)


def generate_getter(cl: ClassIR, attr: str, rtype: RType, emitter: Emitter) -> None:
    attr_field = emitter.attr(attr)
    emitter.emit_line("static PyObject *")
    emitter.emit_line(
        "{}({} *self, void *closure)".format(
            getter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
        )
    )
    emitter.emit_line("{")
    attr_expr = f"self->{attr_field}"

    # HACK: Don't consider refcounted values as always defined, since it's possible to
    #       access uninitialized values via 'gc.get_objects()'. Accessing non-refcounted
    #       values is benign.
    always_defined = cl.is_always_defined(attr) and not rtype.is_refcounted

    if not always_defined:
        emitter.emit_undefined_attr_check(rtype, attr_expr, "==", "self", attr, cl, unlikely=True)
        emitter.emit_line("PyErr_SetString(PyExc_AttributeError,")
        emitter.emit_line(f'    "attribute {repr(attr)} of {repr(cl.name)} undefined");')
        emitter.emit_line("return NULL;")
        emitter.emit_line("}")
    emitter.emit_inc_ref(f"self->{attr_field}", rtype)
    emitter.emit_box(f"self->{attr_field}", "retval", rtype, declare_dest=True)
    emitter.emit_line("return retval;")
    emitter.emit_line("}")


def generate_setter(cl: ClassIR, attr: str, rtype: RType, emitter: Emitter) -> None:
    attr_field = emitter.attr(attr)
    emitter.emit_line("static int")
    emitter.emit_line(
        "{}({} *self, PyObject *value, void *closure)".format(
            setter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
        )
    )
    emitter.emit_line("{")

    deletable = cl.is_deletable(attr)
    if not deletable:
        emitter.emit_line("if (value == NULL) {")
        emitter.emit_line("PyErr_SetString(PyExc_AttributeError,")
        emitter.emit_line(
            f'    "{repr(cl.name)} object attribute {repr(attr)} cannot be deleted");'
        )
        emitter.emit_line("return -1;")
        emitter.emit_line("}")

    # HACK: Don't consider refcounted values as always defined, since it's possible to
    #       access uninitialized values via 'gc.get_objects()'. Accessing non-refcounted
    #       values is benign.
    always_defined = cl.is_always_defined(attr) and not rtype.is_refcounted

    if rtype.is_refcounted:
        attr_expr = f"self->{attr_field}"
        if not always_defined:
            emitter.emit_undefined_attr_check(rtype, attr_expr, "!=", "self", attr, cl)
        emitter.emit_dec_ref(f"self->{attr_field}", rtype)
        if not always_defined:
            emitter.emit_line("}")

    if deletable:
        emitter.emit_line("if (value != NULL) {")

    if rtype.is_unboxed:
        emitter.emit_unbox("value", "tmp", rtype, error=ReturnHandler("-1"), declare_dest=True)
    elif is_same_type(rtype, object_rprimitive):
        emitter.emit_line("PyObject *tmp = value;")
    else:
        emitter.emit_cast("value", "tmp", rtype, declare_dest=True)
        emitter.emit_lines("if (!tmp)", "    return -1;")
    emitter.emit_inc_ref("tmp", rtype)
    emitter.emit_line(f"self->{attr_field} = tmp;")
    if rtype.error_overlap and not always_defined:
        emitter.emit_attr_bitmap_set("tmp", "self", rtype, cl, attr)

    if deletable:
        emitter.emit_line("} else")
        emitter.emit_line(f"    self->{attr_field} = {emitter.c_undefined_value(rtype)};")
        if rtype.error_overlap:
            emitter.emit_attr_bitmap_clear("self", rtype, cl, attr)
    emitter.emit_line("return 0;")
    emitter.emit_line("}")


def generate_readonly_getter(
    cl: ClassIR, attr: str, rtype: RType, func_ir: FuncIR, emitter: Emitter
) -> None:
    emitter.emit_line("static PyObject *")
    emitter.emit_line(
        "{}({} *self, void *closure)".format(
            getter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
        )
    )
    emitter.emit_line("{")
    if rtype.is_unboxed:
        emitter.emit_line(
            "{}retval = {}{}((PyObject *) self);".format(
                emitter.ctype_spaced(rtype), NATIVE_PREFIX, func_ir.cname(emitter.names)
            )
        )
        emitter.emit_error_check("retval", rtype, "return NULL;")
        emitter.emit_box("retval", "retbox", rtype, declare_dest=True)
        emitter.emit_line("return retbox;")
    else:
        emitter.emit_line(
            f"return {NATIVE_PREFIX}{func_ir.cname(emitter.names)}((PyObject *) self);"
        )
    emitter.emit_line("}")


def generate_property_setter(
    cl: ClassIR, attr: str, arg_type: RType, func_ir: FuncIR, emitter: Emitter
) -> None:
    emitter.emit_line("static int")
    emitter.emit_line(
        "{}({} *self, PyObject *value, void *closure)".format(
            setter_name(cl, attr, emitter.names), cl.struct_name(emitter.names)
        )
    )
    emitter.emit_line("{")
    if arg_type.is_unboxed:
        emitter.emit_unbox("value", "tmp", arg_type, error=ReturnHandler("-1"), declare_dest=True)
        emitter.emit_line(
            f"{NATIVE_PREFIX}{func_ir.cname(emitter.names)}((PyObject *) self, tmp);"
        )
    else:
        emitter.emit_line(
            f"{NATIVE_PREFIX}{func_ir.cname(emitter.names)}((PyObject *) self, value);"
        )
    emitter.emit_line("return 0;")
    emitter.emit_line("}")


def has_managed_dict(cl: ClassIR, emitter: Emitter) -> bool:
    """Should the class get the Py_TPFLAGS_MANAGED_DICT flag?"""
    # On 3.11 and earlier the flag doesn't exist and we use
    # tp_dictoffset instead.  If a class inherits from Exception, the
    # flag conflicts with tp_dictoffset set in the base class.
    return (
        emitter.capi_version >= (3, 12)
        and cl.has_dict
        and cl.builtin_base != "PyBaseExceptionObject"
    )
