/* libvcruntime's SEH helpers store type_info's vftable into a local type_info and only call
   through it when RTTI matching happens, which the payload never does. A stub vtable is enough,
   and it keeps the C++ typeinfo/undname machinery (and with it libucrt) out of the link. */
namespace {

void noop(void *) {}
int never(const void *) { return 0; }

}  // namespace

extern "C" const void *const umbra_type_info_vftable[8] = {
    (const void *)noop, (const void *)noop, (const void *)noop, (const void *)noop,
    (const void *)never, (const void *)never,
};

#pragma comment(linker, "/alternatename:??_7type_info@@6B@=umbra_type_info_vftable")
