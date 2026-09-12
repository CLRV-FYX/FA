/* ============================================================
 * FA <-> CPython 桥接：把 CPython 嵌入 FA 进程，
 * 因此 numpy / pandas / torch 等一切 Python 扩展库都可以直接调用。
 * ============================================================ */
#include "fa_runtime.h"

#if FA_HAS_PYTHON
#include <Python.h>

static int fa_py_ready = 0;

static void py_decref(void *p) { if (p) Py_DECREF((PyObject *)p); }

int64_t fa_py_init(void) {
    if (fa_py_ready) return 1;
    if (!Py_IsInitialized()) {
        Py_Initialize();
        PyRun_SimpleString(
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "try:\n"
            "    sys.stdout.reconfigure(encoding='utf-8')\n"
            "    sys.stderr.reconfigure(encoding='utf-8')\n"
            "except Exception:\n"
            "    pass\n");
    }
    fa_py_decref = py_decref;
    fa_py_ready = 1;
    return 1;
}

static PyObject *unwrap(void *p) { return p ? (PyObject *)p : Py_None; }

void *fa_py_import(FaStr *name) {
    fa_py_init();
    PyObject *m = PyImport_ImportModule(name ? name->data : "");
    if (!m) { PyErr_Clear(); return NULL; }
    return (void *)m;                     /* 新引用，交给 FA 的 rc 管理 */
}

void *fa_py_eval(FaStr *code) {
    fa_py_init();
    if (!code) return NULL;
    PyObject *g = PyEval_GetBuiltins();
    if (!g) g = PyDict_New();
    PyObject *r = PyRun_String(code->data, Py_eval_input,
                               PyModule_GetDict(PyImport_AddModule("__main__")), NULL);
    if (!r) { PyErr_Print(); PyErr_Clear(); return NULL; }
    return (void *)r;
}

int64_t fa_py_exec(FaStr *code) {
    fa_py_init();
    if (!code) return 0;
    int r = PyRun_SimpleString(code->data);
    PyErr_Clear();
    return r == 0 ? 1 : 0;
}

void *fa_py_attr(void *obj, FaStr *name) {
    fa_py_init();
    PyObject *o = unwrap(obj);
    PyObject *r = PyObject_GetAttrString(o, name ? name->data : "");
    if (!r) { PyErr_Clear(); return NULL; }
    return (void *)r;
}

void *fa_py_callv(void *obj, FaStr *method, void *args) {
    fa_py_init();
    PyObject *o = unwrap(obj);
    PyObject *fn = NULL;
    if (method && method->len) {
        fn = PyObject_GetAttrString(o, method->data);
        if (!fn) { PyErr_Clear(); return NULL; }
    } else {
        fn = o; Py_INCREF(fn);
    }
    PyObject *tup = NULL;
    if (args) {
        FaVec *v = (FaVec *)args;
        tup = PyTuple_New((Py_ssize_t)v->len);
        for (int64_t i = 0; i < v->len; i++)
            PyTuple_SetItem(tup, (Py_ssize_t)i, unwrap((void *)v->data[i]));
    } else {
        tup = PyTuple_New(0);
    }
    PyObject *r = PyObject_Call(fn, tup, NULL);
    Py_DECREF(tup); Py_DECREF(fn);
    if (!r) { PyErr_Print(); PyErr_Clear(); return NULL; }
    return (void *)r;
}

FaStr *fa_py_to_str(void *obj) {
    fa_py_init();
    PyObject *o = unwrap(obj);
    PyObject *s = PyObject_Str(o);
    if (!s) { PyErr_Clear(); return fa_str_from_cstr(""); }
    Py_ssize_t n = 0;
    const char *data = PyUnicode_AsUTF8AndSize(s, &n);
    FaStr *r = fa_str_new(data ? data : "", (int64_t)n);
    Py_DECREF(s);
    return r;
}

int64_t fa_py_to_i64(void *obj) {
    fa_py_init();
    long long v = PyLong_AsLongLong(unwrap(obj));
    if (PyErr_Occurred()) { PyErr_Clear(); return 0; }
    return (int64_t)v;
}

double fa_py_to_f64(void *obj) {
    fa_py_init();
    double v = PyFloat_AsDouble(unwrap(obj));
    if (PyErr_Occurred()) { PyErr_Clear(); return 0.0; }
    return v;
}

void *fa_py_from_i64(int64_t v) { fa_py_init(); return (void *)PyLong_FromLongLong((long long)v); }
void *fa_py_from_f64(double v) { fa_py_init(); return (void *)PyFloat_FromDouble(v); }
void *fa_py_from_str(FaStr *s) {
    fa_py_init();
    return (void *)PyUnicode_FromStringAndSize(s ? s->data : "", (Py_ssize_t)(s ? s->len : 0));
}
void *fa_py_from_vec(void *v) {
    fa_py_init();
    FaVec *vv = (FaVec *)v;
    PyObject *lst = PyList_New(vv ? (Py_ssize_t)vv->len : 0);
    if (!vv) return (void *)lst;
    for (int64_t i = 0; i < vv->len; i++) {
        PyList_SetItem(lst, (Py_ssize_t)i, unwrap((void *)vv->data[i]));
    }
    return (void *)lst;
}

__attribute__((destructor)) static void fa_py_fini(void) {
    fa_flush();                     /* 先冲掉 FA 自己的输出缓冲 */
    if (fa_py_ready && Py_IsInitialized()) Py_Finalize();
}

#else

/* 未启用 Python 支持时的桩实现 */
int64_t fa_py_init(void) { return 0; }
void *fa_py_import(FaStr *n) { (void)n; return NULL; }
void *fa_py_eval(FaStr *c) { (void)c; return NULL; }
int64_t fa_py_exec(FaStr *c) { (void)c; return 0; }
void *fa_py_callv(void *o, FaStr *m, void *a) { (void)o; (void)m; (void)a; return NULL; }
void *fa_py_attr(void *o, FaStr *n) { (void)o; (void)n; return NULL; }
FaStr *fa_py_to_str(void *o) { (void)o; return fa_str_from_cstr("<python disabled>"); }
int64_t fa_py_to_i64(void *o) { (void)o; return 0; }
double fa_py_to_f64(void *o) { (void)o; return 0.0; }
void *fa_py_from_i64(int64_t v) { (void)v; return NULL; }
void *fa_py_from_f64(double v) { (void)v; return NULL; }
void *fa_py_from_str(FaStr *s) { (void)s; return NULL; }
void *fa_py_from_vec(void *v) { (void)v; return NULL; }

#endif
