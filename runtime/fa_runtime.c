/* FA 运行时核心：内存/引用计数/字符串/向量/哈希映射/IO/系统接口 */
#include "fa_runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <unistd.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdarg.h>
#include <signal.h>
#include <ucontext.h>

/* Python / Java 桥接的引用释放钩子（由对应桥接模块注册） */
void (*fa_py_decref)(void *) = NULL;
void (*fa_jvm_decref)(void *) = NULL;

/* ============================================================ 描述符表 */
#define FA_MAX_DESC 512
static int64_t *fa_desc_table[FA_MAX_DESC];          /* 字段 (偏移, kind) 列表：释放用（兜底） */
static void (*fa_retain_table[FA_MAX_DESC])(void *); /* 逐字段各加一次引用：拷贝用 */
static void (*fa_drop_table[FA_MAX_DESC])(void *);   /* 逐字段各释放一次引用 */

void fa_register_desc(int64_t id, int64_t *desc) {
    if (id >= 0 && id < FA_MAX_DESC) fa_desc_table[id] = desc;
}

void fa_register_retain(int64_t id, void (*fn)(void *)) {
    if (id >= 0 && id < FA_MAX_DESC) fa_retain_table[id] = fn;
}

void fa_register_drop(int64_t id, void (*fn)(void *)) {
    if (id >= 0 && id < FA_MAX_DESC) fa_drop_table[id] = fn;
}

/* ============================================================ 内存与 RC */
void *fa_alloc(int64_t size) {
    void *p = malloc(size > 0 ? (size_t)size : 1);
    if (!p) { fa_sys_write(2, "out of memory\n", 14); fa_sys_exit(134); }
    return p;
}

void fa_free(void *p) { free(p); }

static void fa_drop_desc(void *p, int64_t desc_id) {
    /* 优先用编译器生成的 __fa_drop_<T>：它知道
         - 嵌套结构体是内联的（扁平描述符会去读错字段）
         - 数组字段要按元素循环
         - 枚举要先看 tag，只释放当前变体的载荷
       扁平 (偏移, kind) 表只是没注册函数时的兜底。 */
    if (desc_id >= 0 && desc_id < FA_MAX_DESC && fa_drop_table[desc_id]) {
        fa_drop_table[desc_id](p);
        return;
    }
    int64_t *d = (desc_id >= 0 && desc_id < FA_MAX_DESC) ? fa_desc_table[desc_id] : NULL;
    if (!d) return;
    char *base = (char *)p;
    for (int64_t i = 0; d[i] >= 0; i += 2) {
        int64_t off = d[i];
        int64_t kind = d[i + 1];
        void *field = *(void **)(base + off);
        if (field) fa_rc_dec(field, kind);
    }
}

void fa_rc_inc(void *p) {
    if (!p) return;
    int64_t *rc = (int64_t *)((char *)p - 0);   /* rc 是对象首个字段 */
    if (rc[0] < 0) return;                       /* 静态/永生对象 */
    rc[0]++;
}

/* 按「元素 kind」给容器元素加引用。
   结构体**没有引用计数头**——它的前 8 字节就是第一个字段。
   以前容器一律调 fa_rc_inc，等于把结构体的第一个字段 ++ 了一遍：
   Vec<Q{n:i64,m:i64}> 里 push Q{5,6}，读出来变成 {6,6}（静默数据损坏）。
   现在结构体走编译器生成的 __fa_retain_<T>（逐字段各加一次引用），
   纯数据结构体（FA_K_BOX）没有内部引用，什么都不做。 */
static void fa_agg_inc(void *p, int64_t kind) {
    if (!p) return;
    if (kind == FA_K_NONE || kind == FA_K_BOX) return;
    if (kind >= FA_K_BOXED_STRUCT) {
        void (*f)(void *) = fa_retain_table[kind - FA_K_BOXED_STRUCT];
        if (f) f(p);
        return;
    }
    if (kind >= FA_K_STRUCT_DESC_BASE) {
        void (*f)(void *) = fa_retain_table[kind - FA_K_STRUCT_DESC_BASE];
        if (f) f(p);
        return;
    }
    fa_rc_inc(p);
}


/* 数组（定长 [T; N]）的批量增减引用。
   fn != NULL  -> 元素是**内联**的结构体，对每个元素地址调用 fn（drop / retain）；
   fn == NULL  -> 元素是引用计数对象（str/Vec/Map/pyobj/jobj），按 kind 处理。
   编译器为「结构体里的数组字段」和「数组变量离开作用域」都走这里，
   这样就不必在生成的汇编里手写循环。 */
void fa_drop_arr(void *base, int64_t count, int64_t esz, int64_t kind, void (*fn)(void *)) {
    if (!base || count <= 0 || esz <= 0) return;
    char *p = (char *)base;
    for (int64_t i = 0; i < count; i++) {
        if (fn) { fn(p + i * esz); continue; }
        void *e = NULL;
        memcpy(&e, p + i * esz, sizeof(void *));
        if (e) fa_rc_dec(e, kind);
    }
}

void fa_retain_arr(void *base, int64_t count, int64_t esz, int64_t kind, void (*fn)(void *)) {
    if (!base || count <= 0 || esz <= 0) return;
    char *p = (char *)base;
    for (int64_t i = 0; i < count; i++) {
        if (fn) { fn(p + i * esz); continue; }
        void *e = NULL;
        memcpy(&e, p + i * esz, sizeof(void *));
        if (e) fa_agg_inc(e, kind);
    }
}

/* ---------------------------------------------------------------------------
   向量元素存储宽度
   Vec<bool> / Vec<u8> / Vec<i8> / Vec<char> 这类窄元素按 1 字节紧凑存储，
   i16/u16 按 2 字节、i32/u32 按 4 字节。对外传值仍然是 uint64_t（ABI 不变），
   读写时做零扩展 / 符号扩展。只有整数 / bool / char 走窄路径；
   浮点与结构体保持 8 字节，免得动到已有的 BITCAST 与装箱逻辑。
--------------------------------------------------------------------------- */
static int64_t vec_esz(const FaVec *v) {
    int64_t e = v->esz;
    if (e != 1 && e != 2 && e != 4) return 8;
    return e;
}

static uint64_t vec_load(const FaVec *v, int64_t i) {
    int64_t e = vec_esz(v);
    const unsigned char *p = (const unsigned char *)v->data + (size_t)i * (size_t)e;
    if (e == 8) { uint64_t x; memcpy(&x, p, 8); return x; }
    if (e == 1) return v->sgn ? (uint64_t)(int64_t)(int8_t)*p : (uint64_t)*p;
    if (e == 2) { uint16_t x; memcpy(&x, p, 2);
                  return v->sgn ? (uint64_t)(int64_t)(int16_t)x : (uint64_t)x; }
    { uint32_t x; memcpy(&x, p, 4);
      return v->sgn ? (uint64_t)(int64_t)(int32_t)x : (uint64_t)x; }
}

static void vec_store(FaVec *v, int64_t i, uint64_t val) {
    int64_t e = vec_esz(v);
    unsigned char *p = (unsigned char *)v->data + (size_t)i * (size_t)e;
    if (e == 8) { memcpy(p, &val, 8); return; }
    if (e == 1) { *p = (unsigned char)val; return; }
    if (e == 2) { uint16_t x = (uint16_t)val; memcpy(p, &x, 2); return; }
    { uint32_t x = (uint32_t)val; memcpy(p, &x, 4); }
}

void fa_rc_dec(void *p, int64_t kind) {
    if (!p) return;
    /* 结构体没有 rc 头。以前这里对所有 kind 先做 `--rc[0] > 0` 判断，
       等于把结构体第一个字段当引用计数改了：纯数据结构体永远释放不掉（泄漏），
       第一个字段是小整数时又会提前 free（堆破坏）。三类结构体 kind 必须绕过。 */
    if (kind == FA_K_BOX) { free(p); return; }                 /* 装箱的纯数据结构体 */
    if (kind >= FA_K_BOXED_STRUCT) {                           /* 容器里装箱的含引用结构体 */
        fa_drop_desc(p, kind - FA_K_BOXED_STRUCT);
        free(p);
        return;
    }
    if (kind >= FA_K_STRUCT_DESC_BASE) {                       /* 内联/嵌套结构体：只释放字段 */
        fa_drop_desc(p, kind - FA_K_STRUCT_DESC_BASE);
        return;
    }
    int64_t *rc = (int64_t *)p;
    if (rc[0] < 0) return;
    if (--rc[0] > 0) return;
    switch (kind) {
        case FA_K_STR: free(p); break;
        case FA_K_VEC: {
            FaVec *v = (FaVec *)p;
            if (v->kind) {
                for (int64_t i = 0; i < v->len; i++)
                    fa_rc_dec((void *)(uintptr_t)vec_load(v, i), v->kind);
            }
            free(v->data); free(v); break;
        }
        case FA_K_MAP: {
            FaMap *m = (FaMap *)p;
            for (int64_t i = 0; i < m->cap; i++) {
                if (m->entries[i].state == 1) {
                    if (m->kkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].key, m->kkind);
                    if (m->vkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].val, m->vkind);
                }
            }
            free(m->entries); free(m); break;
        }
        case FA_K_PY: if (fa_py_decref) fa_py_decref(p); break;
        case FA_K_JOBJ: if (fa_jvm_decref) fa_jvm_decref(p); break;
        default:
            break;
    }
}

/* ============================================================ 字符串 */
FaStr *fa_str_new(const char *s, int64_t len) {
    FaStr *r = (FaStr *)fa_alloc((int64_t)sizeof(FaStr) + len + 1);
    r->rc = 1; r->len = len;
    if (s && len) memcpy(r->data, s, (size_t)len);
    r->data[len] = 0;
    return r;
}

FaStr *fa_str_from_cstr(const char *s) { return fa_str_new(s, s ? (int64_t)strlen(s) : 0); }

FaStr *fa_str_concat(FaStr *a, FaStr *b) {
    if (!a) a = fa_str_from_cstr("");
    if (!b) b = fa_str_from_cstr("");
    int64_t n = a->len + b->len;
    FaStr *r = (FaStr *)fa_alloc((int64_t)sizeof(FaStr) + n + 1);
    r->rc = 1; r->len = n;
    memcpy(r->data, a->data, (size_t)a->len);
    memcpy(r->data + a->len, b->data, (size_t)b->len);
    r->data[n] = 0;
    return r;
}

int64_t fa_str_len(FaStr *s) { return s ? s->len : 0; }

int64_t fa_str_eq(FaStr *a, FaStr *b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    if (a->len != b->len) return 0;
    return memcmp(a->data, b->data, (size_t)a->len) == 0 ? 1 : 0;
}

int64_t fa_str_cmp(FaStr *a, FaStr *b) {
    if (!a) a = fa_str_from_cstr("");
    if (!b) b = fa_str_from_cstr("");
    int64_t n = a->len < b->len ? a->len : b->len;
    int c = memcmp(a->data, b->data, (size_t)n);
    if (c) return c;
    return (int)(a->len - b->len);
}

FaStr *fa_str_slice(FaStr *s, int64_t a, int64_t b) {
    if (!s) return fa_str_from_cstr("");
    if (a < 0) a = 0;
    if (b > s->len) b = s->len;
    if (b < a) b = a;
    return fa_str_new(s->data + a, b - a);
}

static void fmt_i64(char *buf, int64_t v, int64_t *out_len) {
    char tmp[24]; int i = 0;
    int neg = v < 0;
    uint64_t u = neg ? (uint64_t)(-(v + 1)) + 1 : (uint64_t)v;
    if (u == 0) tmp[i++] = '0';
    while (u) { tmp[i++] = (char)('0' + (u % 10)); u /= 10; }
    int64_t p = 0;
    if (neg) buf[p++] = '-';
    while (i) buf[p++] = tmp[--i];
    *out_len = p;
}

FaStr *fa_str_of_i64(int64_t v) {
    char buf[32]; int64_t n = 0;
    fmt_i64(buf, v, &n);
    return fa_str_new(buf, n);
}


/* 预分配 len 字节的字符串（内容未初始化，末尾补 NUL） */
static FaStr *fa_str_new_len(int64_t len) {
    FaStr *r = (FaStr *)fa_alloc((int64_t)sizeof(FaStr) + len + 1);
    r->rc = 1; r->len = len; r->data[len] = 0;
    return r;
}

/* ---------------------------------------------------------------------------
   动态字符串构造器 + 统一的值格式化
   ---------------------------------------------------------------------------
   以前「容器转字符串」用的是固定 1KB 缓冲 + snprintf 累加偏移，有两个硬伤：
     * 超过 1KB **静默截断**（打印 200 个元素的 Vec 只能看到前面一截）；
     * snprintf 返回的是「本应写入的长度」，p 一旦越过 cap，下一轮的
       `cap - p` 变成负数并被转成巨大的 size_t —— 直接堆溢出。
   `fa_vec_join` 更糟：它把每个元素都当成 FaStr* 解引用，
   于是 Vec<i64>[3,1,2].join("-") 会把整数 3 当指针用 -> 段错误。
   现在两者都走这个可增长构造器，并按元素类型码 (FaVec.ety) 正确格式化。
--------------------------------------------------------------------------- */
typedef struct { char *p; size_t len, cap; } FaSb;

static void sb_init(FaSb *b) {
    b->cap = 128; b->len = 0;
    b->p = (char *)fa_alloc((int64_t)b->cap);
    b->p[0] = 0;
}
static void sb_free(FaSb *b) { fa_free(b->p); b->p = NULL; b->len = b->cap = 0; }
static void sb_reserve(FaSb *b, size_t extra) {
    if (b->len + extra + 1 <= b->cap) return;
    size_t nc = b->cap;
    while (nc < b->len + extra + 1) nc *= 2;
    char *np = (char *)fa_alloc((int64_t)nc);
    memcpy(np, b->p, b->len + 1);
    fa_free(b->p);
    b->p = np; b->cap = nc;
}
static void sb_put(FaSb *b, const char *s, size_t n) {
    if (!n) return;
    sb_reserve(b, n);
    memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = 0;
}
static void sb_puts(FaSb *b, const char *s) { sb_put(b, s, strlen(s)); }
static void sb_ch(FaSb *b, char c) { sb_reserve(b, 1); b->p[b->len++] = c; b->p[b->len] = 0; }
static void sb_fmt(FaSb *b, const char *fmt, ...) {
    char tmp[96];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n < sizeof(tmp)) { sb_put(b, tmp, (size_t)n); return; }
    char *big = (char *)fa_alloc((int64_t)n + 1);
    va_start(ap, fmt);
    vsnprintf(big, (size_t)n + 1, fmt, ap);
    va_end(ap);
    sb_put(b, big, (size_t)n);
    fa_free(big);
}

/* 与 fa_str_of_f64 完全一致的浮点格式（保证 print(x) 与 print(vec) 里的数字长得一样） */
static int fmt_f64_into(char *buf, size_t cap, double v) {
    int n = snprintf(buf, cap, "%.14g", v);
    if (n < 0) return 0;
    int has = 0;
    for (int i = 0; i < n && (size_t)i < cap; i++)
        if (buf[i] == '.' || buf[i] == 'e' || buf[i] == 'n' || buf[i] == 'i') has = 1;
    if (!has && (size_t)n + 3 <= cap) { buf[n++] = '.'; buf[n++] = '0'; buf[n] = 0; }
    return n;
}

/* 带转义的字符串字面量（含引号/换行的内容不会把外层格式撑破） */
static void sb_quote(FaSb *b, const char *s, size_t n) {
    sb_ch(b, '"');
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
            case '"':  sb_puts(b, "\\\""); break;
            case '\\': sb_puts(b, "\\\\"); break;
            case '\n': sb_puts(b, "\\n");  break;
            case '\r': sb_puts(b, "\\r");  break;
            case '\t': sb_puts(b, "\\t");  break;
            default:
                if (c < 0x20) sb_fmt(b, "\\x%02X", c);
                else sb_ch(b, (char)c);
        }
    }
    sb_ch(b, '"');
}

#define FA_FMT_MAX_DEPTH 8
static void fa_fmt_vec(FaSb *b, const FaVec *v, int depth);
static void fa_fmt_map(FaSb *b, const FaMap *m, int depth);

/* 把容器里一个元素的原始 64 位值按类型码格式化。
   quote_str = 1 时字符串/字符带引号（print 容器用），0 时原样输出（join 用）。 */
static void fa_fmt_elem(FaSb *b, uint64_t raw, int64_t ty, int64_t sgn,
                        int depth, int quote_str) {
    switch (ty) {
        case FA_TY_STR: {
            FaStr *s = (FaStr *)(uintptr_t)raw;
            if (!s) { sb_puts(b, quote_str ? "\"\"" : ""); break; }
            if (quote_str) sb_quote(b, s->data, (size_t)s->len);
            else sb_put(b, s->data, (size_t)s->len);
            break;
        }
        case FA_TY_FLOAT: {
            double d; memcpy(&d, &raw, 8);
            char t[64]; fmt_f64_into(t, sizeof(t), d); sb_puts(b, t);
            break;
        }
        case FA_TY_BOOL: sb_puts(b, raw ? "true" : "false"); break;
        case FA_TY_CHAR: {
            char c = (char)(unsigned char)raw;
            if (!quote_str) { sb_ch(b, c); break; }
            sb_ch(b, '\'');
            if (c == '\'' || c == '\\') sb_ch(b, '\\');
            if (c >= 0x20 && c != 0x7F) sb_ch(b, c); else sb_fmt(b, "\\x%02X", (unsigned char)c);
            sb_ch(b, '\'');
            break;
        }
        case FA_TY_VEC:
            if (depth < FA_FMT_MAX_DEPTH) fa_fmt_vec(b, (const FaVec *)(uintptr_t)raw, depth + 1);
            else sb_puts(b, "[...]");
            break;
        case FA_TY_MAP:
            if (depth < FA_FMT_MAX_DEPTH) fa_fmt_map(b, (const FaMap *)(uintptr_t)raw, depth + 1);
            else sb_puts(b, "{...}");
            break;
        case FA_TY_PTR:   sb_fmt(b, "%p", (void *)(uintptr_t)raw); break;
        case FA_TY_PYOBJ: case FA_TY_JOBJ: sb_puts(b, "<obj>"); break;
        case FA_TY_STRUCT: case FA_TY_ENUM: case FA_TY_ARR: sb_puts(b, "{...}"); break;
        case FA_TY_ANY:   sb_fmt(b, "%llu", (unsigned long long)raw); break;
        default:          /* FA_TY_INT：vec_load 已按 esz/sgn 扩展过 */
            if (sgn) sb_fmt(b, "%lld", (long long)(int64_t)raw);
            else     sb_fmt(b, "%llu", (unsigned long long)raw);
    }
}

static void fa_fmt_vec(FaSb *b, const FaVec *v, int depth) {
    if (!v) { sb_puts(b, "[]"); return; }
    sb_ch(b, '[');
    for (int64_t i = 0; i < v->len; i++) {
        if (i) sb_puts(b, ", ");
        fa_fmt_elem(b, vec_load(v, i), v->ety, v->sgn, depth, 1);
    }
    sb_ch(b, ']');
}

static void fa_fmt_map(FaSb *b, const FaMap *m, int depth) {
    if (!m) { sb_puts(b, "{}"); return; }
    sb_ch(b, '{');
    int first = 1;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->entries[i].state != 1) continue;
        if (!first) sb_puts(b, ", ");
        first = 0;
        fa_fmt_elem(b, m->entries[i].key, m->kty, 1, depth, 1);
        sb_puts(b, ": ");
        fa_fmt_elem(b, m->entries[i].val, m->vty, 1, depth, 1);
    }
    sb_ch(b, '}');
}

/* ==================== 容器增强 ==================== */
static int cmp_i64(const void *a, const void *b) {
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return (x > y) - (x < y);
}
static int cmp_f64(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    if (x != x) return (y != y) ? 0 : 1;          /* NaN 排到最后 */
    if (y != y) return -1;
    return (x > y) - (x < y);
}
static int cmp_strp(const void *a, const void *b) {
    FaStr *x = *(FaStr **)a, *y = *(FaStr **)b;
    if (!x) return y ? -1 : 0;
    if (!y) return 1;
    size_t n = x->len < y->len ? (size_t)x->len : (size_t)y->len;
    int r = n ? memcmp(x->data, y->data, n) : 0;
    if (r) return r;
    return (x->len > y->len) - (x->len < y->len);
}

/* 窄元素无法直接用 qsort 排：先展开成 int64 临时数组，排完再收回去 */
static void sort_i64_narrow(FaVec *v) {
    int64_t n = v->len;
    int64_t *t = (int64_t *)fa_alloc(n * 8);
    for (int64_t i = 0; i < n; i++) t[i] = (int64_t)vec_load(v, i);
    qsort(t, (size_t)n, 8, cmp_i64);
    for (int64_t i = 0; i < n; i++) vec_store(v, i, (uint64_t)t[i]);
    fa_free(t);
}
void fa_vec_sort_i64(FaVec *v) {
    if (!v || v->len < 2) return;
    if (vec_esz(v) != 8) { sort_i64_narrow(v); return; }
    qsort(v->data, (size_t)v->len, 8, cmp_i64);
}
void fa_vec_sort_f64(FaVec *v) { if (v && v->len > 1) qsort(v->data, (size_t)v->len, 8, cmp_f64); }
void fa_vec_sort_str(FaVec *v) { if (v && v->len > 1) qsort(v->data, (size_t)v->len, 8, cmp_strp); }

void fa_vec_reverse(FaVec *v) {
    if (!v) return;
    for (int64_t i = 0, j = v->len - 1; i < j; i++, j--) {
        uint64_t t = vec_load(v, i);
        vec_store(v, i, vec_load(v, j));
        vec_store(v, j, t);
    }
}

FaStr *fa_vec_join(FaVec *v, FaStr *sep) {
    /* 按元素类型格式化后再拼接：Vec<i64>/Vec<f64>/Vec<bool>/Vec<char> 都能 join，
       字符串元素**不加引号**（"a".join 的语义）。 */
    FaSb b; sb_init(&b);
    if (v) {
        const char *sp = (sep && sep->len) ? sep->data : "";
        int64_t sl = sep ? sep->len : 0;
        for (int64_t i = 0; i < v->len; i++) {
            if (i && sl) sb_put(&b, sp, (size_t)sl);
            fa_fmt_elem(&b, vec_load(v, i), v->ety, v->sgn, 0, 0);
        }
    }
    FaStr *r = fa_str_new(b.p, (int64_t)b.len);
    sb_free(&b);
    return r;
}

int64_t fa_vec_sum_i64(FaVec *v) {
    int64_t s = 0;
    if (v) for (int64_t i = 0; i < v->len; i++) s += (int64_t)vec_load(v, i);
    return s;
}
double fa_vec_sum_f64(FaVec *v) {
    double s = 0.0;
    if (v) for (int64_t i = 0; i < v->len; i++) { uint64_t b = vec_load(v, i); s += *(double *)&b; }
    return s;
}
int64_t fa_vec_min_i64(FaVec *v) {
    if (!v || !v->len) return 0;
    int64_t m = (int64_t)vec_load(v, 0);
    for (int64_t i = 1; i < v->len; i++) if ((int64_t)vec_load(v, i) < m) m = (int64_t)vec_load(v, i);
    return m;
}
int64_t fa_vec_max_i64(FaVec *v) {
    if (!v || !v->len) return 0;
    int64_t m = (int64_t)vec_load(v, 0);
    for (int64_t i = 1; i < v->len; i++) if ((int64_t)vec_load(v, i) > m) m = (int64_t)vec_load(v, i);
    return m;
}
double fa_vec_min_f64(FaVec *v) {
    if (!v || !v->len) return 0.0;
    uint64_t b0 = vec_load(v, 0); double m = *(double *)&b0;
    for (int64_t i = 1; i < v->len; i++) { uint64_t b = vec_load(v, i); if (*(double *)&b < m) m = *(double *)&b; }
    return m;
}
double fa_vec_max_f64(FaVec *v) {
    if (!v || !v->len) return 0.0;
    uint64_t b0 = vec_load(v, 0); double m = *(double *)&b0;
    for (int64_t i = 1; i < v->len; i++) { uint64_t b = vec_load(v, i); if (*(double *)&b > m) m = *(double *)&b; }
    return m;
}
int64_t fa_vec_index_of(FaVec *v, uint64_t val) {
    if (!v) return -1;
    for (int64_t i = 0; i < v->len; i++) if (vec_load(v, i) == val) return i;
    return -1;
}

/* ==================== 字符串增强 ==================== */
FaStr *fa_str_repeat(FaStr *s, int64_t n) {
    if (!s || n <= 0 || !s->len) return fa_str_new("", 0);
    int64_t total = s->len * n;
    FaStr *out = fa_str_new_len(total);
    for (int64_t i = 0; i < n; i++) memcpy(out->data + i * s->len, s->data, (size_t)s->len);
    out->data[total] = 0;
    return out;
}

int64_t fa_str_count(FaStr *s, FaStr *sub) {
    if (!s || !sub || !sub->len || !s->len) return 0;
    int64_t n = 0, pos = 0;
    while (pos + sub->len <= s->len) {
        if (memcmp(s->data + pos, sub->data, (size_t)sub->len) == 0) { n++; pos += sub->len; }
        else pos++;
    }
    return n;
}

/* 运行时自己新建一个 FaStr 再塞进 Vec 时用这个。
   fa_vec_push 会按元素 kind 加一次引用（容器自己持有的那份），所以
   「创建时的那份」必须在这里还掉，否则每个元素都多一次引用、永远释放不了：
   ASan 实测 `"FA 语言".split(" ")` 漏 2 个 FaStr，args() 每个参数漏一个。 */
static void vec_push_new_str(FaVec *v, FaStr *s) {
    fa_vec_push(v, (uint64_t)(uintptr_t)s);
    fa_rc_dec(s, FA_K_STR);
}

FaVec *fa_str_lines(FaStr *s) {
    FaVec *v = fa_vec_new(FA_K_STR, 8, 0, FA_TY_STR);
    if (!s) return v;
    int64_t start = 0;
    for (int64_t i = 0; i <= s->len; i++) {
        if (i == s->len || s->data[i] == '\n') {
            int64_t end = i;
            if (end > start && s->data[end - 1] == '\r') end--;
            vec_push_new_str(v, fa_str_new(s->data + start, end - start));
            start = i + 1;
        }
    }
    return v;
}

static int is_space_ch(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f';
}
FaStr *fa_str_trim_start(FaStr *s) {
    if (!s) return fa_str_new("", 0);
    int64_t a = 0;
    while (a < s->len && is_space_ch(s->data[a])) a++;
    return fa_str_new(s->data + a, s->len - a);
}
FaStr *fa_str_trim_end(FaStr *s) {
    if (!s) return fa_str_new("", 0);
    int64_t b = s->len;
    while (b > 0 && is_space_ch(s->data[b - 1])) b--;
    return fa_str_new(s->data, b);
}

FaStr *fa_i64_base(int64_t v, int64_t base, int64_t upper) {
    static const char *dl = "0123456789abcdefghijklmnopqrstuvwxyz";
    static const char *du = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
    const char *d = upper ? du : dl;
    char buf[72];
    int pos = 70;
    uint64_t u;
    int neg = 0;
    if (base < 2) base = 10;
    if (v < 0 && base == 10) { u = (uint64_t)(-(v + 1)) + 1; neg = 1; }
    else u = (uint64_t)v;
    if (u == 0) { buf[pos--] = '0'; }
    while (u) { buf[pos--] = d[u % (uint64_t)base]; u /= (uint64_t)base; }
    if (neg) buf[pos--] = '-';
    return fa_str_new(buf + pos + 1, 70 - pos);
}

int64_t fa_sign_i64(int64_t v) { return (v > 0) - (v < 0); }
double  fa_sign_f64(double v)  { return (v > 0.0) - (v < 0.0); }
int64_t fa_clamp_i64(int64_t v, int64_t lo, int64_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
double  fa_clamp_f64(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

/* ==================== 命令行参数 ==================== */
static int64_t g_argc = 0;
static char **g_argv = NULL;
void fa_set_args(int64_t argc, char **argv) { g_argc = argc; g_argv = argv; }

FaVec *fa_args(void) {
    FaVec *v = fa_vec_new(FA_K_STR, 8, 0, FA_TY_STR);
    for (int64_t i = 0; i < g_argc; i++) {
        vec_push_new_str(v, fa_str_from_cstr(g_argv ? g_argv[i] : ""));
    }
    return v;
}

FaStr *fa_str_of_f64(double v) {
    char buf[64];
    int n = fmt_f64_into(buf, sizeof(buf), v);
    return fa_str_new(buf, n);
}

FaStr *fa_str_of_bool(int64_t v) { return fa_str_from_cstr(v ? "true" : "false"); }

FaStr *fa_str_of_char(int64_t v) { char b[4] = {(char)v, 0, 0, 0}; return fa_str_new(b, 1); }

FaStr *fa_str_of_ptr(void *v) {
    char buf[32];
    int n = snprintf(buf, sizeof(buf), "%p", v);
    return fa_str_new(buf, n);
}

/* 码点 -> UTF-8 字符串（1~4 字节）。超出合法范围或代理区一律按 U+FFFD 处理，
   保证「任意 i64 都能变成合法 UTF-8」，不会产出畸形序列。 */
FaStr *fa_str_chr(int64_t cp) {
    unsigned char b[4];
    int64_t n;
    if (cp < 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) cp = 0xFFFD;
    if (cp < 0x80) {
        b[0] = (unsigned char)cp; n = 1;
    } else if (cp < 0x800) {
        b[0] = (unsigned char)(0xC0 | (cp >> 6));
        b[1] = (unsigned char)(0x80 | (cp & 0x3F)); n = 2;
    } else if (cp < 0x10000) {
        b[0] = (unsigned char)(0xE0 | (cp >> 12));
        b[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        b[2] = (unsigned char)(0x80 | (cp & 0x3F)); n = 3;
    } else {
        b[0] = (unsigned char)(0xF0 | (cp >> 18));
        b[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
        b[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        b[3] = (unsigned char)(0x80 | (cp & 0x3F)); n = 4;
    }
    return fa_str_new((char *)b, n);
}

int64_t fa_str_byte(FaStr *s, int64_t i) {
    if (!s || i < 0 || i >= s->len) return 0;
    return (unsigned char)s->data[i];
}

int64_t fa_str_find(FaStr *s, FaStr *sub) {
    if (!s || !sub) return -1;
    if (sub->len == 0) return 0;
    if (sub->len > s->len) return -1;
    for (int64_t i = 0; i + sub->len <= s->len; i++) {
        if (memcmp(s->data + i, sub->data, (size_t)sub->len) == 0) return i;
    }
    return -1;
}

int64_t fa_str_starts(FaStr *s, FaStr *p) {
    if (!s || !p) return 0;
    if (p->len > s->len) return 0;
    return memcmp(s->data, p->data, (size_t)p->len) == 0 ? 1 : 0;
}

int64_t fa_str_ends(FaStr *s, FaStr *p) {
    if (!s || !p) return 0;
    if (p->len > s->len) return 0;
    return memcmp(s->data + s->len - p->len, p->data, (size_t)p->len) == 0 ? 1 : 0;
}

FaStr *fa_str_trim(FaStr *s) {
    if (!s) return fa_str_from_cstr("");
    int64_t a = 0, b = s->len;
    while (a < b && (unsigned char)s->data[a] <= ' ') a++;
    while (b > a && (unsigned char)s->data[b - 1] <= ' ') b--;
    return fa_str_new(s->data + a, b - a);
}

FaStr *fa_str_upper(FaStr *s) {
    if (!s) return fa_str_from_cstr("");
    FaStr *r = fa_str_new(s->data, s->len);
    for (int64_t i = 0; i < r->len; i++)
        if (r->data[i] >= 'a' && r->data[i] <= 'z') r->data[i] -= 32;
    return r;
}

FaStr *fa_str_lower(FaStr *s) {
    if (!s) return fa_str_from_cstr("");
    FaStr *r = fa_str_new(s->data, s->len);
    for (int64_t i = 0; i < r->len; i++)
        if (r->data[i] >= 'A' && r->data[i] <= 'Z') r->data[i] += 32;
    return r;
}

FaStr *fa_str_replace(FaStr *s, FaStr *a, FaStr *b) {
    if (!s || !a || a->len == 0) return fa_str_new(s ? s->data : "", s ? s->len : 0);
    size_t cap = (size_t)s->len * 2 + 32;
    char *out = (char *)fa_alloc((int64_t)cap);
    size_t n = 0;
    for (int64_t i = 0; i < s->len;) {
        if (i + a->len <= s->len && memcmp(s->data + i, a->data, (size_t)a->len) == 0) {
            if (n + (size_t)b->len + 1 > cap) {
                cap = (n + (size_t)b->len + 1) * 2;
                char *t = (char *)fa_alloc((int64_t)cap);
                memcpy(t, out, n); fa_free(out); out = t;
            }
            memcpy(out + n, b->data, (size_t)b->len); n += (size_t)b->len;
            i += a->len;
        } else {
            if (n + 2 > cap) {
                cap *= 2;
                char *t = (char *)fa_alloc((int64_t)cap);
                memcpy(t, out, n); fa_free(out); out = t;
            }
            out[n++] = s->data[i++];
        }
    }
    FaStr *r = fa_str_new(out, (int64_t)n);
    fa_free(out);
    return r;
}

FaVec *fa_str_split(FaStr *s, FaStr *sep) {
    FaVec *v = fa_vec_new(FA_K_STR, 8, 0, FA_TY_STR);
    if (!s) return v;
    if (!sep || sep->len == 0) {
        for (int64_t i = 0; i < s->len; i++)
            vec_push_new_str(v, fa_str_new(s->data + i, 1));
        return v;
    }
    int64_t start = 0;
    for (int64_t i = 0; i + sep->len <= s->len; i++) {
        if (memcmp(s->data + i, sep->data, (size_t)sep->len) == 0) {
            vec_push_new_str(v, fa_str_new(s->data + start, i - start));
            start = i + sep->len;
            i = start - 1;
        }
    }
    vec_push_new_str(v, fa_str_new(s->data + start, s->len - start));
    return v;
}

/* ------------------------------------------------------------------ UTF-8 码点
   FA 的 char 就是一个字节（u8），所以 s[i] / s.chars() 拿到的都是**字节** ——
   对中文这类多字节文本，「第 i 个字符」得自己解码。下面这组函数把解码放进运行时：
   码点用 int64_t 表示（char 装不下 > 255 的码点），非法/截断的字节序列按单字节
   码点处理（Latin-1 兜底），所以任何输入都不会失败，也不会读到缓冲区外面。 */
static int64_t utf8_dec(const char *p, int64_t len, int64_t i, int64_t *next) {
    unsigned char c = (unsigned char)p[i];
    int64_t need, cp;
    if (c < 0x80) { *next = i + 1; return c; }
    else if ((c & 0xE0) == 0xC0) { need = 1; cp = c & 0x1F; }
    else if ((c & 0xF0) == 0xE0) { need = 2; cp = c & 0x0F; }
    else if ((c & 0xF8) == 0xF0) { need = 3; cp = c & 0x07; }
    else { *next = i + 1; return c; }                 /* 非法起始字节 */
    if (i + need >= len) { *next = i + 1; return c; } /* 序列被截断 */
    for (int64_t k = 1; k <= need; k++) {
        unsigned char cc = (unsigned char)p[i + k];
        if ((cc & 0xC0) != 0x80) { *next = i + 1; return c; }
        cp = (cp << 6) | (cc & 0x3F);
    }
    *next = i + need + 1;
    return cp;
}

/* 第 ci 个码点的**字节**下标（ci 超出码点数就返回 len，方便切片时夹紧） */
static int64_t utf8_byte_off(FaStr *s, int64_t ci) {
    int64_t i = 0, n = 0, next;
    while (i < s->len) {
        if (n == ci) return i;
        utf8_dec(s->data, s->len, i, &next);
        i = next; n++;
    }
    return s->len;
}

int64_t fa_str_char_len(FaStr *s) {
    if (!s) return 0;
    int64_t n = 0, i = 0, next;
    while (i < s->len) { utf8_dec(s->data, s->len, i, &next); i = next; n++; }
    return n;
}

int64_t fa_str_char_at(FaStr *s, int64_t idx) {
    if (!s) return 0;
    int64_t i = 0, n = 0, next, cp;
    while (i < s->len) {
        cp = utf8_dec(s->data, s->len, i, &next);
        if (n == idx) return cp;
        i = next; n++;
    }
    if (idx < 0) return 0;
    fa_bounds_error(idx, n);                          /* 和下标越界同一套报错 */
    return 0;
}

FaVec *fa_str_codepoints(FaStr *s) {
    FaVec *v = fa_vec_new(FA_K_NONE, 8, 1, FA_TY_INT);
    if (!s) return v;
    int64_t i = 0, next;
    while (i < s->len) {
        fa_vec_push(v, (uint64_t)utf8_dec(s->data, s->len, i, &next));
        i = next;
    }
    return v;
}

FaStr *fa_str_slice_chars(FaStr *s, int64_t a, int64_t b) {
    if (!s) return fa_str_from_cstr("");
    int64_t n = fa_str_char_len(s);
    if (a < 0) a = 0;
    if (b > n) b = n;
    if (b < a) b = a;
    int64_t ba = utf8_byte_off(s, a), bb = utf8_byte_off(s, b);
    return fa_str_new(s->data + ba, bb - ba);
}

FaVec *fa_str_chars(FaStr *s) {
    FaVec *v = fa_vec_new(FA_K_NONE, 1, 0, FA_TY_CHAR);
    if (!s) return v;
    for (int64_t i = 0; i < s->len; i++) fa_vec_push(v, (uint64_t)(unsigned char)s->data[i]);
    return v;
}

int64_t fa_str_to_i64(FaStr *s) {
    if (!s) return 0;
    return strtoll(s->data, NULL, 10);
}

double fa_str_to_f64(FaStr *s) { return s ? strtod(s->data, NULL) : 0.0; }

char *fa_str_cstr(FaStr *s) { return s ? s->data : (char *)""; }

FaStr *fa_container_to_str(void *v, int64_t kind) {
    /* kind: 1 = Vec, 2 = Map（与 codegen 的调用约定一致） */
    FaSb b; sb_init(&b);
    if (kind == 1) fa_fmt_vec(&b, (const FaVec *)v, 0);
    else           fa_fmt_map(&b, (const FaMap *)v, 0);
    FaStr *r = fa_str_new(b.p, (int64_t)b.len);
    sb_free(&b);
    return r;
}

/* ============================================================ 输出缓冲 */
#define FA_OBUF 65536
static char fa_obuf[FA_OBUF];
static size_t fa_olen = 0;
static int fa_tty = -1;

void fa_flush(void) {
    /* 先把 C 库 stdio 缓冲区里的内容冲掉，
       这样 printf / std::cout 的输出会按程序顺序出现在 FA 的输出之前 */
    fflush(stdout);
    if (fa_olen) { fa_sys_write(1, fa_obuf, (int64_t)fa_olen); fa_olen = 0; }
}

static void obuf_put(const char *s, size_t n) {
    if (fa_olen + n > FA_OBUF - 8) fa_flush();
    if (n > FA_OBUF - 8) { fa_sys_write(1, s, (int64_t)n); return; }
    memcpy(fa_obuf + fa_olen, s, n); fa_olen += n;
}

static int fa_force_flush = -1;
static void maybe_flush_nl(void) {
    if (fa_tty < 0) fa_tty = isatty(1);
    if (fa_force_flush < 0) fa_force_flush = getenv("FA_FLUSH") != NULL;
    if (fa_tty || fa_force_flush) fa_flush();
}

void fa_print_str(FaStr *s) { if (s && s->len) obuf_put(s->data, (size_t)s->len); }
void fa_print_i64(int64_t v) { char b[24]; int64_t n; fmt_i64(b, v, &n); obuf_put(b, (size_t)n); }
void fa_print_f64(double v) {
    FaStr *s = fa_str_of_f64(v); fa_print_str(s); fa_free(s);
}
/* 以前是 fa_print_str(fa_str_of_bool(v))：堆上分配一个 "true"/"false" 却从不
   释放，于是**每打印一个布尔值就漏 20 字节**（fa_print_f64 / fa_print_ptr 都
   记得 fa_free，就这里漏了）。直接往输出缓冲写死字符串，一次分配都不需要。 */
void fa_print_bool(int64_t v) { obuf_put(v ? "true" : "false", v ? 4 : 5); }
void fa_print_char(int64_t v) { char b[1] = {(char)v}; obuf_put(b, 1); }
void fa_print_ptr(void *v) { FaStr *s = fa_str_of_ptr(v); fa_print_str(s); fa_free(s); }
void fa_print_nl(void) { obuf_put("\n", 1); maybe_flush_nl(); }

/* ============================================================ 向量 */
FaVec *fa_vec_new(int64_t kind, int64_t esz, int64_t sgn, int64_t ety) {
    FaVec *v = (FaVec *)fa_alloc((int64_t)sizeof(FaVec));
    v->rc = 1; v->len = 0; v->cap = 8; v->kind = kind;
    v->esz = (esz == 1 || esz == 2 || esz == 4) ? esz : 8;
    v->sgn = (v->esz == 8) ? 0 : (sgn ? 1 : 0);
    v->ety = ety;
    v->data = (uint64_t *)fa_alloc(8 * (size_t)v->esz);
    return v;
}

static void vec_grow(FaVec *v) {
    int64_t nc = v->cap * 2;
    int64_t e = vec_esz(v);
    uint64_t *nd = (uint64_t *)fa_alloc(nc * e);
    memcpy(nd, v->data, (size_t)v->len * (size_t)e);
    fa_free(v->data); v->data = nd; v->cap = nc;
}

int64_t fa_vec_len(FaVec *v) { return v ? v->len : 0; }

void fa_vec_push(FaVec *v, uint64_t val) {
    if (!v) return;
    if (v->len == v->cap) vec_grow(v);
    fa_agg_inc((void *)(uintptr_t)val, v->kind);
    vec_store(v, v->len++, val);
}

uint64_t fa_vec_get(FaVec *v, int64_t i) {
    if (!v || i < 0 || i >= v->len) {
        fa_sys_write(2, "vec index out of range\n", 23); fa_sys_exit(1);
    }
    return vec_load(v, i);
}

void fa_vec_set(FaVec *v, int64_t i, uint64_t val) {
    if (!v || i < 0 || i >= v->len) {
        fa_sys_write(2, "vec index out of range\n", 23); fa_sys_exit(1);
    }
    uint64_t old = vec_load(v, i);
    fa_agg_inc((void *)(uintptr_t)val, v->kind);
    vec_store(v, i, val);
    if (v->kind) fa_rc_dec((void *)(uintptr_t)old, v->kind);
}

uint64_t fa_vec_pop(FaVec *v) {
    if (!v || v->len == 0) {
        fa_sys_write(2, "pop from empty vec\n", 19); fa_sys_exit(1);
    }
    return vec_load(v, --v->len);
}

/* 拷贝一个装箱的聚合元素。

   盒子（FA_K_BOX / FA_K_BOXED_STRUCT）**不带引用计数**：fa_agg_inc 对它是空操作，
   而释放路径会 free 掉盒子。所以两个 Vec 共享同一个盒子 = 释放两次
   （实测 glibc 报 "double free detected in tcache 2"）。必须另分配一份盒子、
   按字节拷过来，再给里面的引用计数字段各加一次引用。 */
static uint64_t fa_clone_boxed(uint64_t val, int64_t box_size, int64_t kind) {
    void *nb;
    if (!val) return 0;
    nb = fa_alloc(box_size);
    memcpy(nb, (void *)(uintptr_t)val, (size_t)box_size);
    fa_agg_inc(nb, kind);           /* BOXED_STRUCT：字段各加一次；BOX：空操作 */
    return (uint64_t)(uintptr_t)nb;
}

/* v.copy()：另起一份表。

   - 标量元素按字节拷；
   - str / Vec / Map 这类引用计数元素各加一次引用（str 不可变，共享没问题；
     嵌套的 Vec/Map 拷的是**把手**，不是深拷贝 —— 想彻底独立要自己再逐层 copy）；
   - 装箱的 struct / enum 元素另分配盒子（见 fa_clone_boxed）。 */
FaVec *fa_vec_clone(FaVec *v, int64_t box_size) {
    int64_t i;
    if (!v) return fa_vec_new(0, 8, 0, FA_TY_INT);
    FaVec *r = fa_vec_new(v->kind, v->esz, v->sgn, v->ety);
    for (i = 0; i < v->len; i++) {
        uint64_t val = vec_load(v, i);
        if (r->len == r->cap) vec_grow(r);
        if (box_size > 0) {
            val = fa_clone_boxed(val, box_size, v->kind);
        } else {
            fa_agg_inc((void *)(uintptr_t)val, v->kind);
        }
        vec_store(r, r->len++, val);
    }
    return r;
}

void fa_vec_clear(FaVec *v) {
    if (!v) return;
    if (v->kind) for (int64_t i = 0; i < v->len; i++) fa_rc_dec((void *)vec_load(v, i), v->kind);
    v->len = 0;
}

/* ============================================================ 映射 */
static uint64_t fa_hash_u64(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

static uint64_t fa_hash_str(FaStr *s) {
    if (!s) return 0;
    uint64_t h = 1469598103934665603ULL;
    for (int64_t i = 0; i < s->len; i++) { h ^= (unsigned char)s->data[i]; h *= 1099511628211ULL; }
    return h;
}

/* 下标越界：把长度和下标一起打出来，用户才知道是哪儿算错了。
   v[i] / v.get(i) / s[i] / a[i] / s.char_at(i) 共用这一句。 */
void fa_bounds_error(int64_t idx, int64_t len) {
    char buf[128];
    int n;
    fa_flush();
    n = snprintf(buf, sizeof(buf),
                 "panic: 下标越界 (index out of range)：长度 %lld，下标 %lld\n",
                 (long long)len, (long long)idx);
    if (n < 0) n = 0;
    if (n > (int)sizeof(buf)) n = (int)sizeof(buf);
    fa_sys_write(2, buf, (int64_t)n);
    fa_sys_exit(1);
}

int64_t fa_vec_contains(FaVec *v, uint64_t val) {
    if (!v) return 0;
    if (v->kind == 1) {                     /* str：按内容比较 */
        FaStr *s = (FaStr *)val;
        for (int64_t i = 0; i < v->len; i++) {
            FaStr *e = (FaStr *)vec_load(v, i);
            if (e == s) return 1;
            if (e && s && e->len == s->len &&
                memcmp(e->data, s->data, (size_t)e->len) == 0) return 1;
        }
        return 0;
    }
    /* 数值/指针：按位比较（f64 的位模式相同即认为相等） */
    for (int64_t i = 0; i < v->len; i++) if (vec_load(v, i) == val) return 1;
    return 0;
}

/* 变长时把**同一个** val push n 次：只对标量元素成立。引用计数元素（str/Vec/Map）
   和装箱元素（struct/enum）必须每格新建一份，那条路由编译器发循环
   （codegen.emit_vec_resize_ref），不走这里。 */
void fa_vec_resize(FaVec *v, int64_t n, uint64_t val) {
    if (!v || n < 0) return;
    while (v->len > n) {
        /* fa_vec_pop 只是把值交出来、len--，**不释放**（`v.pop()` 的返回值归调用方）。
           这里没人接，就得自己还掉 —— 以前直接丢弃，截断 Vec<str>/Vec<Vec> 时
           被删掉的元素永远漏在堆上（ASan 实测）。 */
        uint64_t old = fa_vec_pop(v);
        if (v->kind) fa_rc_dec((void *)(uintptr_t)old, v->kind);
    }
    while (v->len < n) fa_vec_push(v, val);
}

FaMap *fa_map_new(int64_t kkind, int64_t vkind, int64_t kty, int64_t vty) {
    FaMap *m = (FaMap *)fa_alloc((int64_t)sizeof(FaMap));
    m->rc = 1; m->len = 0; m->cap = 16; m->kkind = kkind; m->vkind = vkind;
    m->kty = kty; m->vty = vty;
    m->entries = (FaMapEntry *)fa_alloc((int64_t)sizeof(FaMapEntry) * 16);
    memset(m->entries, 0, (size_t)sizeof(FaMapEntry) * 16);
    return m;
}

static void map_grow(FaMap *m) {
    int64_t nc = m->cap * 2;
    FaMapEntry *ne = (FaMapEntry *)fa_alloc((int64_t)sizeof(FaMapEntry) * nc);
    memset(ne, 0, (size_t)sizeof(FaMapEntry) * nc);
    FaMapEntry *oe = m->entries;
    int64_t oc = m->cap;
    m->entries = ne; m->cap = nc; m->len = 0;
    for (int64_t i = 0; i < oc; i++) {
        if (oe[i].state == 1) fa_map_set(m, oe[i].key, oe[i].val);
    }
    fa_free(oe);
}

static int64_t map_probe(FaMap *m, uint64_t key, int64_t for_insert) {
    uint64_t h = (m->kkind == FA_K_STR) ? fa_hash_str((FaStr *)key) : fa_hash_u64(key);
    int64_t mask = m->cap - 1;
    int64_t i = (int64_t)(h & (uint64_t)mask);
    int64_t first_tomb = -1;
    for (int64_t probe = 0; probe < m->cap; probe++) {
        FaMapEntry *e = &m->entries[(i + probe) & mask];
        if (e->state == 0) return for_insert ? ((first_tomb >= 0) ? first_tomb : ((i + probe) & mask)) : -1;
        if (e->state == -1) { if (first_tomb < 0) first_tomb = (i + probe) & mask; continue; }
        if (m->kkind == FA_K_STR) {
            if (fa_str_eq((FaStr *)e->key, (FaStr *)key)) return (i + probe) & mask;
        } else if (e->key == key) return (i + probe) & mask;
    }
    return for_insert ? first_tomb : -1;
}

int64_t fa_map_len(FaMap *m) { return m ? m->len : 0; }

uint64_t fa_map_get(FaMap *m, uint64_t key) {
    if (!m) return 0;
    int64_t i = map_probe(m, key, 0);
    return (i >= 0 && m->entries[i].state == 1) ? m->entries[i].val : 0;
}

int64_t fa_map_has(FaMap *m, uint64_t key) {
    if (!m) return 0;
    int64_t i = map_probe(m, key, 0);
    return (i >= 0 && m->entries[i].state == 1) ? 1 : 0;
}

/* m.copy()：另起一份字典。按原表的槽位顺序重放 set —— 内容完全一致，但**遍历
   顺序不保证和原表相同**（Map 的遍历顺序本来就是哈希槽顺序，重放时扩容/落槽
   可能不一样；实测 {"a":1,"b":2} 拷完再 set 一个键，顺序就成了 c,b,a）。
   装箱的键 / 值同 Vec 一样要另分配盒子（fa_map_set 只会 fa_agg_inc，
   对不带引用计数的盒子等于没拷）。 */
FaMap *fa_map_clone(FaMap *m, int64_t kbox, int64_t vbox) {
    int64_t i;
    if (!m) return fa_map_new(0, 0, FA_TY_INT, FA_TY_INT);
    FaMap *r = fa_map_new(m->kkind, m->vkind, m->kty, m->vty);
    for (i = 0; i < m->cap; i++) {
        uint64_t k, v;
        if (m->entries[i].state != 1) continue;
        k = m->entries[i].key;
        v = m->entries[i].val;
        if (kbox > 0) k = fa_clone_boxed(k, kbox, m->kkind);
        if (vbox > 0) v = fa_clone_boxed(v, vbox, m->vkind);
        fa_map_set(r, k, v);
    }
    return r;
}

void fa_map_set(FaMap *m, uint64_t key, uint64_t val) {
    if (!m) return;
    if ((m->len + 1) * 10 > m->cap * 7) map_grow(m);
    int64_t i = map_probe(m, key, 1);
    if (i < 0) return;
    if (m->entries[i].state == 1) {
        uint64_t ok = m->entries[i].key, ov = m->entries[i].val;
        fa_agg_inc((void *)(uintptr_t)key, m->kkind);
        fa_agg_inc((void *)(uintptr_t)val, m->vkind);
        m->entries[i].key = key; m->entries[i].val = val;
        if (m->kkind) fa_rc_dec((void *)(uintptr_t)ok, m->kkind);
        if (m->vkind) fa_rc_dec((void *)(uintptr_t)ov, m->vkind);
    } else {
        fa_agg_inc((void *)(uintptr_t)key, m->kkind);
        fa_agg_inc((void *)(uintptr_t)val, m->vkind);
        m->entries[i].key = key; m->entries[i].val = val; m->entries[i].state = 1;
        m->len++;
    }
}

void fa_map_del(FaMap *m, uint64_t key) {
    if (!m) return;
    int64_t i = map_probe(m, key, 0);
    if (i < 0 || m->entries[i].state != 1) return;
    if (m->kkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].key, m->kkind);
    if (m->vkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].val, m->vkind);
    m->entries[i].state = -1; m->entries[i].key = 0; m->entries[i].val = 0;
    m->len--;
}

uint64_t fa_map_val_at(FaMap *m, int64_t idx) {
    /* 第 idx 个「已占用」槽的值。
       以前直接返回 entries[idx].val —— 哈希表里有空槽与墓碑，
       于是 values(m) 会读到别的键的值甚至 0（Map{1:100,2:200} 给出 [0,100]）。 */
    if (!m || idx < 0 || idx >= m->len) return 0;
    int64_t c = 0;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->entries[i].state == 1) { if (c == idx) return m->entries[i].val; c++; }
    }
    return 0;
}

void fa_map_clear(FaMap *m) {
    if (!m) return;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->entries[i].state == 1) {
            if (m->kkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].key, m->kkind);
            if (m->vkind) fa_rc_dec((void *)(uintptr_t)m->entries[i].val, m->vkind);
        }
        m->entries[i].state = 0; m->entries[i].key = 0; m->entries[i].val = 0;
    }
    m->len = 0;
}

uint64_t fa_map_key_at(FaMap *m, int64_t idx) {
    if (!m) return 0;
    int64_t c = 0;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->entries[i].state == 1) { if (c == idx) return m->entries[i].key; c++; }
    }
    return 0;
}

/* ============================================================ 系统 */
void fa_panic(FaStr *s) {
    fa_flush();
    fa_sys_write(2, "panic: ", 7);
    if (s && s->len) fa_sys_write(2, s->data, s->len);
    fa_sys_write(2, "\n", 1);
    fa_exit(1);
}

void fa_exit(int64_t code) { fa_flush(); _exit((int)code); }

/* ============================================================ 硬件陷阱 */
/* 整数除以零在 x86 上是 #DE -> SIGFPE，野指针解引用是 SIGSEGV。
   默认行为是内核直接干掉进程（shell 只打一句 "Segmentation fault"），
   用户看不到任何解释 —— 这跟文档承诺的「运行时报出明确的信息并退出」不符。
   这里装上处理器，用**原始系统调用**打一行说明再退出。
   信号处理函数里不能用 stdio / malloc（都不是异步信号安全的），
   所以只冲 FA 自己的输出缓冲，长度也在安装时就预算好。 */
static struct { int sig; const char *msg; int64_t len; } fa_traps[] = {
    { SIGFPE, "panic: 整数除以零 (division by zero)\n", 0 },
    { SIGBUS, "panic: 总线错误（非法内存访问）\n", 0 },
    { SIGSEGV, "panic: 段错误（非法内存访问 / 野指针）\n", 0 },
};

/* 栈溢出必须能报出来：主栈耗尽时内核没法在栈上放信号帧，处理器根本跑不起来，
   进程就这么静默消失（shell 只看到一个奇怪的退出码，用户什么都看不到）。
   所以给信号单独备一块栈，并用 SA_ONSTACK 让处理器在那上面跑。 */
#define FA_ALTSTACK_BYTES (256 * 1024)
static char fa_altstack_mem[FA_ALTSTACK_BYTES];

static const char fa_msg_stackovf[] =
    "panic: 栈溢出（递归太深 / 局部变量太大）\n";
static int64_t fa_msg_stackovf_len = 0;

/* x86-64 glibc 的 ucontext 里 RSP 在 gregs 的下标（REG_RSP，见 sys/ucontext.h） */
#define FA_REG_RSP 15

static void fa_trap(int sig, siginfo_t *si, void *uc) {
    const char *msg = "panic: 致命硬件陷阱\n";
    int64_t len = 27;
    for (unsigned i = 0; i < sizeof(fa_traps) / sizeof(fa_traps[0]); i++) {
        if (fa_traps[i].sig == sig) { msg = fa_traps[i].msg; len = fa_traps[i].len; break; }
    }
    if (sig == SIGSEGV && si != NULL && uc != NULL) {
        /* 出错地址紧贴栈指针下方 = call/push 撞上了栈底的守护页，是栈溢出，
           不是野指针。野指针解引用的地址一般离 RSP 很远。 */
        unsigned long rsp =
            (unsigned long)((ucontext_t *)uc)->uc_mcontext.gregs[FA_REG_RSP];
        unsigned long addr = (unsigned long)(uintptr_t)si->si_addr;
        if (addr <= rsp && rsp - addr < (1UL << 16)) {
            msg = fa_msg_stackovf;
            len = fa_msg_stackovf_len;
        }
    }
    if (fa_olen) { fa_sys_write(1, fa_obuf, (int64_t)fa_olen); fa_olen = 0; }
    fa_sys_write(2, msg, len);
    fa_sys_exit(1);
}

__attribute__((constructor))
static void fa_install_traps(void) {
    stack_t ss;
    ss.ss_sp = fa_altstack_mem;
    ss.ss_size = sizeof(fa_altstack_mem);
    ss.ss_flags = 0;
    sigaltstack(&ss, NULL);
    fa_msg_stackovf_len = (int64_t)strlen(fa_msg_stackovf);
    for (unsigned i = 0; i < sizeof(fa_traps) / sizeof(fa_traps[0]); i++) {
        struct sigaction sa;
        fa_traps[i].len = (int64_t)strlen(fa_traps[i].msg);
        memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = fa_trap;
        sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
        sigemptyset(&sa.sa_mask);
        sigaction(fa_traps[i].sig, &sa, NULL);
    }
}

double fa_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

void fa_sleep_ms(int64_t ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000; ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

FaStr *fa_read_line(void) {
    fa_flush();
    size_t cap = 128, n = 0;
    char *buf = (char *)fa_alloc((int64_t)cap);
    int c;
    while ((c = fgetc(stdin)) != EOF && c != '\n') {
        if (n + 2 > cap) { cap *= 2; char *t = (char *)fa_alloc((int64_t)cap); memcpy(t, buf, n); fa_free(buf); buf = t; }
        buf[n++] = (char)c;
    }
    if (c == EOF && n == 0) { fa_free(buf); return fa_str_from_cstr(""); }
    FaStr *r = fa_str_new(buf, (int64_t)n);
    fa_free(buf);
    return r;
}

FaStr *fa_file_read(FaStr *path) {
    if (!path) return fa_str_from_cstr("");
    FILE *f = fopen(path->data, "rb");
    if (!f) return fa_str_from_cstr("");
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    char *buf = (char *)fa_alloc((int64_t)sz + 1);
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    FaStr *r = fa_str_new(buf, (int64_t)got);
    fa_free(buf);
    return r;
}

int64_t fa_file_write(FaStr *path, FaStr *content) {
    if (!path) return 0;
    FILE *f = fopen(path->data, "wb");
    if (!f) return 0;
    size_t n = content ? (size_t)content->len : 0;
    if (n) fwrite(content->data, 1, n, f);
    fclose(f);
    return 1;
}

FaStr *fa_system_capture(FaStr *cmd) {
    if (!cmd) return fa_str_from_cstr("");
    FILE *p = popen(cmd->data, "r");
    if (!p) return fa_str_from_cstr("");
    size_t cap = 4096, n = 0;
    char *buf = (char *)fa_alloc((int64_t)cap);
    while (fgets(buf + n, (int)(cap - n), p)) {
        n = strlen(buf);
        if (n + 4096 > cap) { cap *= 2; char *t = (char *)fa_alloc((int64_t)cap); memcpy(t, buf, n); fa_free(buf); buf = t; }
    }
    pclose(p);
    FaStr *r = fa_str_new(buf, (int64_t)n);
    fa_free(buf);
    return r;
}

FaStr *fa_env(FaStr *name) {
    if (!name) return fa_str_from_cstr("");
    const char *v = getenv(name->data);
    return fa_str_from_cstr(v ? v : "");
}

static uint64_t fa_rng_state = 88172645463325252ULL;
int64_t fa_random(void) {
    uint64_t x = fa_rng_state;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    fa_rng_state = x;
    return (int64_t)(x >> 11);
}

int64_t fa_ipow(int64_t a, int64_t b) {
    int64_t r = 1;
    while (b > 0) { if (b & 1) r *= a; a *= a; b >>= 1; }
    return r;
}
int64_t fa_imin(int64_t a, int64_t b) { return a < b ? a : b; }
int64_t fa_imax(int64_t a, int64_t b) { return a > b ? a : b; }
int64_t fa_gcd(int64_t a, int64_t b) { while (b) { int64_t t = a % b; a = b; b = t; } return a < 0 ? -a : a; }

/* ============================================================ 动态库 */
void *fa_dl_open(const char *path) {
    void *h = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    if (!h) { fa_sys_write(2, "dlopen failed: ", 15); fa_sys_write(2, path, (int64_t)strlen(path)); fa_sys_write(2, "\n", 1); }
    return h;
}

int64_t fa_dl_bind(void *handle, void **slot, const char *name) {
    if (!handle) return 0;
    void *s = dlsym(handle, name);
    if (!s) return 0;
    *slot = s;
    return 1;
}

/* 程序退出时刷新输出缓冲 */
__attribute__((destructor)) static void fa_cleanup(void) { fa_flush(); }
