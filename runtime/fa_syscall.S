/* ============================================================
 * FA 运行时 —— 手写 x86-64 汇编热路径（Linux syscall, System V）
 * 不依赖 libc 包装，减少函数调用开销。
 * ============================================================ */
    .intel_syntax noprefix
    .text
    .globl  fa_sys_write
    .globl  fa_sys_read
    .globl  fa_sys_exit
    .globl  fa_strlen_asm
    .globl  fa_memcmp_asm

/* ssize_t fa_sys_write(int64_t fd, const char* buf, int64_t count) */
fa_sys_write:
    mov     rax, 1
    syscall
    ret

/* ssize_t fa_sys_read(int64_t fd, char* buf, int64_t count) */
fa_sys_read:
    mov     rax, 0
    syscall
    ret

/* void fa_sys_exit(int64_t code)   —— 不返回 */
fa_sys_exit:
    mov     rax, 60
    syscall
    hlt

/* int64_t fa_strlen_asm(const char* s) —— 手写求长，用于输出热路径 */
fa_strlen_asm:
    xor     rax, rax
    test    rdi, rdi
    jz      .Lstrlen_done
.Lstrlen_loop:
    cmp     byte ptr [rdi + rax], 0
    je      .Lstrlen_done
    inc     rax
    jmp     .Lstrlen_loop
.Lstrlen_done:
    ret

/* int64_t fa_memcmp_asm(const void* a, const void* b, int64_t n) */
fa_memcmp_asm:
    xor     rax, rax
    test    rdx, rdx
    jz      .Lmemcmp_done
.Lmemcmp_loop:
    mov     cl, byte ptr [rdi + rax]
    cmp     cl, byte ptr [rsi + rax]
    jne     .Lmemcmp_diff
    inc     rax
    cmp     rax, rdx
    jne     .Lmemcmp_loop
    xor     rax, rax
    ret
.Lmemcmp_diff:
    mov     cl, byte ptr [rdi + rax]
    movzx   rax, cl
    mov     dl, byte ptr [rsi + rax]
    movzx   rdx, dl
    sub     rax, rdx
    ret
.Lmemcmp_done:
    ret

    .section .note.GNU-stack,"",@progbits
