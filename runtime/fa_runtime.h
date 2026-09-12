/* FA 运行时头文件 —— 与编译器 codegen 中引用的符号一一对应 */
#ifndef FA_RUNTIME_H
#define FA_RUNTIME_H

#include <stdint.h>
#include <stddef.h>

/* kind 编码（与 compiler/falang/types.py 保持一致） */
#define FA_K_NONE 0
#define FA_K_STR  1
#define FA_K_VEC  2
#define FA_K_MAP  3
#define FA_K_PY   4
#define FA_K_JOBJ 5
#define FA_K_BOX  6

typedef struct FaStr {
    int64_t  rc;
    int64_t  len;
    char     data[];
} FaStr;

typedef struct FaVec {
    int64_t  rc;
    int64_t  len;
    int64_t  cap;
    int64_t  kind;      /* 元素 kind（引用计数用；0 = 不参与引用计数） */
    uint64_t *data;     /* 必须保持在偏移 32：codegen 内联的 get/set/len 直接用这个偏移 */
    int64_t  esz;       /* 元素存储宽度：1/2/4/8 字节 */
    int64_t  sgn;       /* 窄元素是否有符号（决定零扩展还是符号扩展） */
} FaVec;

typedef struct FaMapEntry {
    uint64_t key;
    uint64_t val;
    int64_t  state;     /* 0 空 1 占用 -1 墓碑 */
} FaMapEntry;

typedef struct FaMap {
    int64_t     rc;
    int64_t     len;
    int64_t     cap;
    int64_t     kkind;
    int64_t     vkind;
    FaMapEntry *entries;
} FaMap;

/* 桥接模块的引用释放钩子 */
extern void (*fa_py_decref)(void *);
extern void (*fa_jvm_decref)(void *);

/* --- 内存 --- */
void *fa_alloc(int64_t size);
void  fa_free(void *p);
void  fa_rc_inc(void *p);
void  fa_rc_dec(void *p, int64_t kind);
void  fa_register_desc(int64_t id, int64_t *desc);

/* --- 字符串 --- */
FaStr *fa_str_new(const char *s, int64_t len);
FaStr *fa_str_from_cstr(const char *s);
FaStr *fa_str_concat(FaStr *a, FaStr *b);
int64_t fa_str_len(FaStr *s);
int64_t fa_str_eq(FaStr *a, FaStr *b);
int64_t fa_str_cmp(FaStr *a, FaStr *b);
FaStr *fa_str_slice(FaStr *s, int64_t a, int64_t b);
FaStr *fa_str_of_i64(int64_t v);
FaStr *fa_str_of_f64(double v);
FaStr *fa_str_of_bool(int64_t v);
FaStr *fa_str_of_char(int64_t v);
FaStr *fa_str_of_ptr(void *v);
int64_t fa_str_byte(FaStr *s, int64_t i);
FaStr  *fa_str_chr(int64_t cp);
int64_t fa_str_find(FaStr *s, FaStr *sub);
FaStr *fa_str_trim(FaStr *s);
FaStr *fa_str_upper(FaStr *s);
FaStr *fa_str_lower(FaStr *s);
FaStr *fa_str_replace(FaStr *s, FaStr *a, FaStr *b);
FaVec *fa_str_split(FaStr *s, FaStr *sep);
FaVec *fa_str_chars(FaStr *s);
int64_t fa_str_to_i64(FaStr *s);
double  fa_str_to_f64(FaStr *s);
char   *fa_str_cstr(FaStr *s);
int64_t fa_str_starts(FaStr *s, FaStr *p);
int64_t fa_str_ends(FaStr *s, FaStr *p);
FaStr *fa_container_to_str(void *v, int64_t kind);

/* --- 输出 --- */
void fa_print_str(FaStr *s);
void fa_print_i64(int64_t v);
void fa_print_f64(double v);
void fa_print_bool(int64_t v);
void fa_print_char(int64_t v);
void fa_print_ptr(void *v);
void fa_print_nl(void);
void fa_flush(void);


/* --- 容器增强：排序 / 反转 / 拼接 --- */
void    fa_vec_sort_i64(FaVec *v);
void    fa_vec_sort_f64(FaVec *v);
void    fa_vec_sort_str(FaVec *v);
void    fa_vec_reverse(FaVec *v);
FaStr  *fa_vec_join(FaVec *v, FaStr *sep);
int64_t fa_vec_sum_i64(FaVec *v);
double  fa_vec_sum_f64(FaVec *v);
int64_t fa_vec_min_i64(FaVec *v);
int64_t fa_vec_max_i64(FaVec *v);
double  fa_vec_min_f64(FaVec *v);
double  fa_vec_max_f64(FaVec *v);
int64_t fa_vec_index_of(FaVec *v, uint64_t val);

/* --- 字符串增强 --- */
FaStr  *fa_str_repeat(FaStr *s, int64_t n);
int64_t fa_str_count(FaStr *s, FaStr *sub);
FaVec  *fa_str_lines(FaStr *s);
FaStr  *fa_str_trim_start(FaStr *s);
FaStr  *fa_str_trim_end(FaStr *s);
FaStr  *fa_i64_base(int64_t v, int64_t base, int64_t upper);

int64_t fa_sign_i64(int64_t v);
double  fa_sign_f64(double v);
int64_t fa_clamp_i64(int64_t v, int64_t lo, int64_t hi);
double  fa_clamp_f64(double v, double lo, double hi);

/* --- 命令行参数 --- */
void    fa_set_args(int64_t argc, char **argv);
FaVec  *fa_args(void);

/* --- 容器 --- */
FaVec *fa_vec_new(int64_t kind, int64_t esz, int64_t sgn);
int64_t fa_vec_len(FaVec *v);
void    fa_vec_push(FaVec *v, uint64_t val);
uint64_t fa_vec_get(FaVec *v, int64_t i);
void    fa_vec_set(FaVec *v, int64_t i, uint64_t val);
uint64_t fa_vec_pop(FaVec *v);
void    fa_vec_clear(FaVec *v);
void    fa_bounds_error(void);
int64_t fa_vec_contains(FaVec *v, uint64_t val);
void    fa_vec_resize(FaVec *v, int64_t n, uint64_t val);

FaMap *fa_map_new(int64_t kkind, int64_t vkind);
int64_t fa_map_len(FaMap *m);
uint64_t fa_map_get(FaMap *m, uint64_t key);
void    fa_map_set(FaMap *m, uint64_t key, uint64_t val);
int64_t fa_map_has(FaMap *m, uint64_t key);
void    fa_map_del(FaMap *m, uint64_t key);
void    fa_map_clear(FaMap *m);
uint64_t fa_map_key_at(FaMap *m, int64_t i);
uint64_t fa_map_val_at(FaMap *m, int64_t i);

/* --- 系统 --- */
void    fa_panic(FaStr *s);
void    fa_exit(int64_t code);
double  fa_now(void);
void    fa_sleep_ms(int64_t ms);
FaStr  *fa_read_line(void);
FaStr  *fa_file_read(FaStr *path);
int64_t fa_file_write(FaStr *path, FaStr *content);
FaStr  *fa_system_capture(FaStr *cmd);
FaStr  *fa_env(FaStr *name);
int64_t fa_random(void);
int64_t fa_ipow(int64_t a, int64_t b);
int64_t fa_imin(int64_t a, int64_t b);
int64_t fa_imax(int64_t a, int64_t b);
int64_t fa_gcd(int64_t a, int64_t b);

/* --- 动态库懒绑定 --- */
void *fa_dl_open(const char *path);
int64_t fa_dl_bind(void *handle, void **slot, const char *name);

/* --- 手写汇编热路径 --- */
int64_t fa_sys_write(int64_t fd, const char *buf, int64_t count);
int64_t fa_sys_read(int64_t fd, char *buf, int64_t count);
void    fa_sys_exit(int64_t code);

/* --- Python 桥 --- */
int64_t fa_py_init(void);
void   *fa_py_import(FaStr *name);
void   *fa_py_eval(FaStr *code);
int64_t fa_py_exec(FaStr *code);
void   *fa_py_callv(void *obj, FaStr *method, void *args);
void   *fa_py_attr(void *obj, FaStr *name);
FaStr  *fa_py_to_str(void *obj);
int64_t fa_py_to_i64(void *obj);
double  fa_py_to_f64(void *obj);
void   *fa_py_from_i64(int64_t v);
void   *fa_py_from_f64(double v);
void   *fa_py_from_str(FaStr *s);
void   *fa_py_from_vec(void *v);

/* --- Java 桥 --- */
int64_t fa_jvm_init(FaStr *classpath);
int64_t fa_jvm_init_default(void);
void   *fa_jvm_find_class(FaStr *name);
int64_t fa_jvm_call_static_i64(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
double  fa_jvm_call_static_f64(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void   *fa_jvm_call_static_obj(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void    fa_jvm_call_static_void(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void   *fa_jvm_new_obj(void *cls, FaStr *sig, int64_t n, uint64_t *args);
int64_t fa_jvm_call_i64(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
double  fa_jvm_call_f64(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void   *fa_jvm_call_obj(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void    fa_jvm_call_void(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args);
void   *fa_jvm_str(FaStr *s);
FaStr  *fa_jvm_to_str(void *obj);
int64_t fa_jvm_to_i64(void *obj);
double  fa_jvm_to_f64(void *obj);

#endif
