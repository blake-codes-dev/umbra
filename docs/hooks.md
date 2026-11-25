# Hook invariants

Four rules, each learned by breaking it. They are cheap to keep and expensive to rediscover.

## 1. The replay tail must not use rax

The replay ends by pushing the resume address and returning into it. Using `mov rax,resume` leaves
the *address* in rax, and rax is the return value of most of the functions worth hooking, so every
hooked call hands its caller a pointer where a result was expected. Use r11 instead:

    mov r11, at + displaced
    push r11
    ret

r11 is volatile, and no continuation depends on it.

## 2. The stub must leave the stack aligned

At the target, rsp is 8 mod 16 because the caller's `call` pushed a return address. Five pushes
take it to 0. Exactly 0x20 of shadow space is what leaves it 0 mod 16 before `call rax`, which is
what the ABI wants: the callee sees 8 mod 16 on entry. 0x28 is one qword too many, and then every
`movaps` spill in the thunk faults against a misaligned frame.

## 3. Never start an install from inside an install

Installing runs Luau, and every Luau call can enter the hook again. The flag that says "already
installed" has to be set *before* the first call into the VM, not after the state work, or the hook
restarts the install from inside itself and the C stack goes.

## 4. Synthetic unwind entries are relative to our own base

`RtlAddFunctionTable` resolves the entries it is given against the base passed with them. The codes
and the table live in the payload, so the base is the payload's module, never the client's. Against
the client base, ntdll reads `-1` and dies delivering the first fault.
