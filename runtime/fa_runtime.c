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

/* Python / Java 桥接的引用释放钩子（由对应桥接模块注册） */
void (*fa_py_decref)(void *) = NULL;
void (*fa_jvm_decref)(void *) = NULL;

/* ============================================================ 描述符表 */
#define FA_MAX_DESC 512
static int64_t *fa_desc_table[FA_MAX_DESC];

void fa_register_desc(int64_t id, int64_t *desc) {
    if (id >= 0 && id < FA_MAX_DESC) fa_desc_table[id] = desc;
}

/* ============================================================ 内存与 RC */
void *fa_alloc(int64_t size) {
    void *p = malloc(size > 0 ? (size_t)size : 1);
    if (!p) { fa_sys_write(2, "out of memory\n", 14); fa_sys_exit(134); }
    return p;
}

void fa_free(void *p) { free(p); }

static void fa_drop_desc(void *p, int64_t desc_id) {
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
    int64_t *rc = (int64_t *)((char *)p - 0);   /* rc 是结构体首个字段 */
    if (rc[0] < 0) return;                       /* 静态/永生对象 */
    rc[0]++;
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
    int64_t *rc = (int64_t *)p;
    if (rc[0] < 0) return;
    if (--rc[0] > 0) return;
    switch (kind) {
        case FA_K_STR: free(p); break;
        case FA_K_VEC: {
            FaVec *v = (FaVec *)p;
            if (v->kind) {
                for (int64_t i = 0; i < v->len; i++) {
                    void *e = (void *)vec_load(v, i);
                    if (e) fa_rc_dec(e, v->kind);
                }
            }
            free(v->data); free(v); break;
        }
        case FA_K_MAP: {
            FaMap *m = (FaMap *)p;
            for (int64_t i = 0; i < m->cap; i++) {
                if (m->entries[i].state == 1) {
                    if (m->kkind) fa_rc_dec((void *)m->entries[i].key, m->kkind);
                    if (m->vkind) fa_rc_dec((void *)m->entries[i].val, m->vkind);
                }
            }
            free(m->entries); free(m); break;
        }
        case FA_K_PY: if (fa_py_decref) fa_py_decref(p); break;
        case FA_K_JOBJ: if (fa_jvm_decref) fa_jvm_decref(p); break;
        case FA_K_BOX: free(p); break;
        default:
            if (kind >= 1000) { fa_drop_desc(p, kind - 1000); free(p); }
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
    if (!v) return fa_str_from_cstr("");
    const char *sp = (sep && sep->len) ? sep->data : "";
    int64_t sl = sep ? sep->len : 0;
    int64_t total = 0;
    for (int64_t i = 0; i < v->len; i++) {
        FaStr *e = (FaStr *)vec_load(v, i);
        total += e ? e->len : 0;
        if (i + 1 < v->len) total += sl;
    }
    FaStr *out = fa_str_new_len(total);
    int64_t pos = 0;
    for (int64_t i = 0; i < v->len; i++) {
        FaStr *e = (FaStr *)vec_load(v, i);
        if (e && e->len) { memcpy(out->data + pos, e->data, (size_t)e->len); pos += e->len; }
        if (i + 1 < v->len && sl) { memcpy(out->data + pos, sp, (size_t)sl); pos += sl; }
    }
    out->data[total] = 0;
    return out;
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

FaVec *fa_str_lines(FaStr *s) {
    FaVec *v = fa_vec_new(1, 8, 0);
    if (!s) return v;
    int64_t start = 0;
    for (int64_t i = 0; i <= s->len; i++) {
        if (i == s->len || s->data[i] == '\n') {
            int64_t end = i;
            if (end > start && s->data[end - 1] == '\r') end--;
            FaStr *line = fa_str_new(s->data + start, end - start);
            fa_vec_push(v, (uint64_t)line);
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
    FaVec *v = fa_vec_new(1, 8, 0);
    for (int64_t i = 0; i < g_argc; i++) {
        FaStr *s = fa_str_from_cstr(g_argv ? g_argv[i] : "");
        fa_vec_push(v, (uint64_t)s);
    }
    return v;
}

FaStr *fa_str_of_f64(double v) {
    char buf[64];
    int n = snprintf(buf, sizeof(buf), "%.14g", v);
    int has = 0;
    for (int i = 0; i < n; i++) if (buf[i] == '.' || buf[i] == 'e' || buf[i] == 'n') has = 1;
    if (!has && n < 60) { buf[n++] = '.'; buf[n++] = '0'; }
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
    FaVec *v = fa_vec_new(FA_K_STR, 8, 0);
    if (!s) return v;
    if (!sep || sep->len == 0) {
        for (int64_t i = 0; i < s->len; i++)
            fa_vec_push(v, (uint64_t)fa_str_new(s->data + i, 1));
        return v;
    }
    int64_t start = 0;
    for (int64_t i = 0; i + sep->len <= s->len; i++) {
        if (memcmp(s->data + i, sep->data, (size_t)sep->len) == 0) {
            fa_vec_push(v, (uint64_t)fa_str_new(s->data + start, i - start));
            start = i + sep->len;
            i = start - 1;
        }
    }
    fa_vec_push(v, (uint64_t)fa_str_new(s->data + start, s->len - start));
    return v;
}

FaVec *fa_str_chars(FaStr *s) {
    FaVec *v = fa_vec_new(FA_K_NONE, 1, 0);
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
    if (!v) return fa_str_from_cstr(kind == 1 ? "[]" : "{}");
    size_t cap = 1024;
    char *b2 = (char *)fa_alloc((int64_t)cap);
    int p = 0;
    if (kind == 1) {
        FaVec *vv = (FaVec *)v;
        p += snprintf(b2 + p, cap - p, "[");
        for (int64_t i = 0; i < vv->len; i++) {
            if (i) p += snprintf(b2 + p, cap - p, ", ");
            if (vv->kind == FA_K_STR) {
                FaStr *s = (FaStr *)vec_load(vv, i);
                p += snprintf(b2 + p, cap - p, "\"%s\"", s ? s->data : "");
            } else if (vv->kind == FA_K_NONE) {
                p += snprintf(b2 + p, cap - p, "%lld", (long long)(int64_t)vec_load(vv, i));
            } else {
                p += snprintf(b2 + p, cap - p, "<obj>");
            }
            if (p >= (int)cap - 64) break;
        }
        p += snprintf(b2 + p, cap - p, "]");
    } else {
        FaMap *m = (FaMap *)v;
        p += snprintf(b2 + p, cap - p, "{");
        int first = 1;
        for (int64_t i = 0; i < m->cap; i++) {
            if (m->entries[i].state != 1) continue;
            if (!first) p += snprintf(b2 + p, cap - p, ", ");
            first = 0;
            if (m->kkind == FA_K_STR) {
                FaStr *k = (FaStr *)m->entries[i].key;
                p += snprintf(b2 + p, cap - p, "\"%s\": ", k ? k->data : "");
            } else {
                p += snprintf(b2 + p, cap - p, "%lld: ", (long long)(int64_t)m->entries[i].key);
            }
            if (m->vkind == FA_K_STR) {
                FaStr *val = (FaStr *)m->entries[i].val;
                p += snprintf(b2 + p, cap - p, "\"%s\"", val ? val->data : "");
            } else {
                p += snprintf(b2 + p, cap - p, "%lld", (long long)(int64_t)m->entries[i].val);
            }
            if (p >= (int)cap - 64) break;
        }
        p += snprintf(b2 + p, cap - p, "}");
    }
    FaStr *r = fa_str_new(b2, p);
    fa_free(b2);
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
void fa_print_bool(int64_t v) { fa_print_str(fa_str_of_bool(v)); }
void fa_print_char(int64_t v) { char b[1] = {(char)v}; obuf_put(b, 1); }
void fa_print_ptr(void *v) { FaStr *s = fa_str_of_ptr(v); fa_print_str(s); fa_free(s); }
void fa_print_nl(void) { obuf_put("\n", 1); maybe_flush_nl(); }

/* ============================================================ 向量 */
FaVec *fa_vec_new(int64_t kind, int64_t esz, int64_t sgn) {
    FaVec *v = (FaVec *)fa_alloc((int64_t)sizeof(FaVec));
    v->rc = 1; v->len = 0; v->cap = 8; v->kind = kind;
    v->esz = (esz == 1 || esz == 2 || esz == 4) ? esz : 8;
    v->sgn = (v->esz == 8) ? 0 : (sgn ? 1 : 0);
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
    if (v->kind) fa_rc_inc((void *)val);
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
    if (v->kind) fa_rc_inc((void *)val);
    vec_store(v, i, val);
    if (v->kind) fa_rc_dec((void *)old, v->kind);
}

uint64_t fa_vec_pop(FaVec *v) {
    if (!v || v->len == 0) {
        fa_sys_write(2, "pop from empty vec\n", 19); fa_sys_exit(1);
    }
    return vec_load(v, --v->len);
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

void fa_bounds_error(void) {
    fa_flush();
    fa_sys_write(2, "index out of range\n", 19);
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

void fa_vec_resize(FaVec *v, int64_t n, uint64_t val) {
    if (!v || n < 0) return;
    while (v->len > n) fa_vec_pop(v);
    while (v->len < n) fa_vec_push(v, val);
}

FaMap *fa_map_new(int64_t kkind, int64_t vkind) {
    FaMap *m = (FaMap *)fa_alloc((int64_t)sizeof(FaMap));
    m->rc = 1; m->len = 0; m->cap = 16; m->kkind = kkind; m->vkind = vkind;
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

void fa_map_set(FaMap *m, uint64_t key, uint64_t val) {
    if (!m) return;
    if ((m->len + 1) * 10 > m->cap * 7) map_grow(m);
    int64_t i = map_probe(m, key, 1);
    if (i < 0) return;
    if (m->entries[i].state == 1) {
        uint64_t ok = m->entries[i].key, ov = m->entries[i].val;
        if (m->kkind) fa_rc_inc((void *)key);
        if (m->vkind) fa_rc_inc((void *)val);
        m->entries[i].key = key; m->entries[i].val = val;
        if (m->kkind) fa_rc_dec((void *)ok, m->kkind);
        if (m->vkind) fa_rc_dec((void *)ov, m->vkind);
    } else {
        if (m->kkind) fa_rc_inc((void *)key);
        if (m->vkind) fa_rc_inc((void *)val);
        m->entries[i].key = key; m->entries[i].val = val; m->entries[i].state = 1;
        m->len++;
    }
}

void fa_map_del(FaMap *m, uint64_t key) {
    if (!m) return;
    int64_t i = map_probe(m, key, 0);
    if (i < 0 || m->entries[i].state != 1) return;
    if (m->kkind) fa_rc_dec((void *)m->entries[i].key, m->kkind);
    if (m->vkind) fa_rc_dec((void *)m->entries[i].val, m->vkind);
    m->entries[i].state = -1; m->entries[i].key = 0; m->entries[i].val = 0;
    m->len--;
}

uint64_t fa_map_val_at(FaMap *m, int64_t i) {
    if (!m || i < 0 || i >= m->len) return 0;
    return m->entries[i].val;
}

void fa_map_clear(FaMap *m) {
    if (!m) return;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->entries[i].state == 1) {
            if (m->kkind) fa_rc_dec((void *)m->entries[i].key, m->kkind);
            if (m->vkind) fa_rc_dec((void *)m->entries[i].val, m->vkind);
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
