/* FA 运行时头文件 —— 与编译器 codegen 中引用的符号一一对应 */
#ifndef FA_RUNTIME_H
#define FA_RUNTIME_H

#include <stdint.h>
#include <stddef.h>

/* kind 编码（与 compiler/falang/types.py 保持一致）—— 决定「怎么释放」 */
#define FA_K_NONE 0
#define FA_K_STR  1
#define FA_K_VEC  2
#define FA_K_MAP  3
#define FA_K_PY   4
#define FA_K_JOBJ 5
#define FA_K_BOX  6                   /* 装箱的纯数据结构体：释放时直接 free */
#define FA_K_STRUCT_DESC_BASE   1000  /* +desc_id：内联/嵌套结构体，释放字段但不 free */
#define FA_K_BOXED_STRUCT       2000  /* +desc_id：容器里装箱的结构体，释放字段 + free */

/* 元素类型编码（与 compiler/falang/types.py 的 ty_code() 一致）—— 决定「怎么显示」
   kind 只够用来做引用计数，分不清 i64 / u64 / f64 / bool / char，
   所以容器额外记一个类型码，to_str / join / print 才能格式化正确。 */
#define FA_TY_INT    0   /* 宽度看 esz，符号看 sgn */
#define FA_TY_FLOAT  1   /* 一律按 double 解释（f32 在表达式里已提升为 f64） */
#define FA_TY_BOOL   2
#define FA_TY_CHAR   3
#define FA_TY_STR    4
#define FA_TY_VEC    5
#define FA_TY_MAP    6
#define FA_TY_STRUCT 7
#define FA_TY_PYOBJ  8
#define FA_TY_JOBJ   9
#define FA_TY_PTR    10
#define FA_TY_ENUM   11
#define FA_TY_ARR    12
#define FA_TY_ANY    13

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
    int64_t  ety;       /* 元素类型码 FA_TY_*（显示用；偏移 56，绝不可挪到 data 之前） */
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
    int64_t     kty;    /* 键类型码 FA_TY_*（显示用） */
    int64_t     vty;    /* 值类型码 FA_TY_*（显示用） */
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
void  fa_register_retain(int64_t id, void (*fn)(void *));
void  fa_register_drop(int64_t id, void (*fn)(void *));

/* 定长数组的批量增减引用（fn != NULL 时元素是内联结构体） */
void  fa_drop_arr(void *base, int64_t count, int64_t esz, int64_t kind, void (*fn)(void *));
void  fa_retain_arr(void *base, int64_t count, int64_t esz, int64_t kind, void (*fn)(void *));

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
/* UTF-8 码点（char 是一个字节，码点用 int64_t） */
int64_t fa_str_char_len(FaStr *s);
int64_t fa_str_char_at(FaStr *s, int64_t idx);
FaVec  *fa_str_codepoints(FaStr *s);
FaStr  *fa_str_slice_chars(FaStr *s, int64_t a, int64_t b);
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
FaVec *fa_vec_new(int64_t kind, int64_t esz, int64_t sgn, int64_t ety);
FaVec *fa_vec_clone(FaVec *v, int64_t box_size);   /* v.copy()；box_size>0 表示元素是装箱的聚合 */
int64_t fa_vec_len(FaVec *v);
void    fa_vec_push(FaVec *v, uint64_t val);
uint64_t fa_vec_get(FaVec *v, int64_t i);
void    fa_vec_set(FaVec *v, int64_t i, uint64_t val);
uint64_t fa_vec_pop(FaVec *v);
void    fa_vec_clear(FaVec *v);
void    fa_bounds_error(int64_t idx, int64_t len);
int64_t fa_vec_contains(FaVec *v, uint64_t val);
void    fa_vec_resize(FaVec *v, int64_t n, uint64_t val);

FaMap *fa_map_new(int64_t kkind, int64_t vkind, int64_t kty, int64_t vty);
FaMap *fa_map_clone(FaMap *m, int64_t kbox, int64_t vbox);   /* m.copy() */
int64_t fa_map_len(FaMap *m);
/* 按**槽位**遍历：fa_map_key_at/val_at 那种「第 idx 个占用槽」每调一次都要从头扫，
   整个 keys() / for k in m 就是 O(n·cap) —— 五万个键实测 12 秒。下面这组是 O(1) 一次，
   调用方自己扫 0..fa_map_cap 并跳过未占用的槽（空槽与墓碑），一趟 O(cap)。 */
int64_t fa_map_cap(FaMap *m);
int64_t fa_map_slot_used(FaMap *m, int64_t i);
uint64_t fa_map_slot_key(FaMap *m, int64_t i);
uint64_t fa_map_slot_val(FaMap *m, int64_t i);
/* 一趟建出所有键 / 值的 Vec（构造参数与 fa_vec_new 一致） */
FaVec *fa_map_keys_vec(FaMap *m, int64_t kind, int64_t esz, int64_t sgn, int64_t ety);
FaVec *fa_map_vals_vec(FaMap *m, int64_t kind, int64_t esz, int64_t sgn, int64_t ety);
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
