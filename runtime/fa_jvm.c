/* ============================================================
 * FA <-> Java 桥接：通过 JNI 把 JVM 嵌入 FA 进程，
 * 因此一切 Java 类库（JDK 类库、Maven jar、自研 jar）都能直接调用。
 * ============================================================ */
#include "fa_runtime.h"

#if FA_HAS_JAVA
#include <jni.h>
#include <string.h>
#include <stdlib.h>

static JavaVM *g_vm = NULL;
static JNIEnv *g_env = NULL;
static int fa_jvm_ready = 0;

static void jvm_decref(void *p) {
    if (p && g_env) (*g_env)->DeleteGlobalRef(g_env, (jobject)p);
}

int64_t fa_jvm_init(FaStr *classpath) {
    if (fa_jvm_ready) return 1;
    JavaVMInitArgs vm_args;
    JavaVMOption opts[2];
    char cp[4096];
    snprintf(cp, sizeof(cp), "-Djava.class.path=%s", classpath ? classpath->data : ".");
    opts[0].optionString = cp;
    opts[1].optionString = "-Xrs";
    vm_args.version = JNI_VERSION_1_8;
    vm_args.nOptions = 2;
    vm_args.options = opts;
    vm_args.ignoreUnrecognized = JNI_TRUE;
    if (JNI_CreateJavaVM(&g_vm, (void **)&g_env, &vm_args) != JNI_OK || !g_env) {
        return 0;
    }
    fa_jvm_decref = jvm_decref;
    fa_jvm_ready = 1;
    return 1;
}

int64_t fa_jvm_init_default(void) {
    const char *cp = getenv("CLASSPATH");
    if (!cp) cp = ".:*";
    FaStr *s = fa_str_from_cstr(cp);
    int64_t r = fa_jvm_init(s);
    fa_free(s);
    return r;
}

void *fa_jvm_find_class(FaStr *name) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    if (!g_env || !name) return NULL;
    /* JNI 的类名用 '/' 分隔；FA 侧允许写 'java.lang.Math' 这种点分形式 */
    char buf[512];
    snprintf(buf, sizeof(buf), "%s", name->data);
    for (char *p = buf; *p; p++) if (*p == '.') *p = '/';
    jclass c = (*g_env)->FindClass(g_env, buf);
    if (!c) { (*g_env)->ExceptionClear(g_env); return NULL; }
    jobject g = (jobject)(*g_env)->NewGlobalRef(g_env, (jobject)c);
    (*g_env)->DeleteLocalRef(g_env, c);
    return (void *)g;
}

static jmethodID get_static(void *cls, FaStr *m, FaStr *sig) {
    if (!cls || !m || !sig) return NULL;
    jmethodID id = (*g_env)->GetStaticMethodID(g_env, (jclass)cls, m->data, sig->data);
    if (!id) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return id;
}

static void *to_global(jobject o) {
    if (!o) return NULL;
    jobject g = (*g_env)->NewGlobalRef(g_env, o);
    (*g_env)->DeleteLocalRef(g_env, o);
    return (void *)g;
}


/* 按 JNI 签名的返回类型分派调用（int 与 long 绝不能混用，否则读到垃圾高位） */
static char jni_ret_kind(FaStr *sig) {
    if (!sig || !sig->data) return 'V';
    const char *p = strchr(sig->data, ')');
    return (p && p[1]) ? p[1] : 'V';
}

static int64_t jni_static_i64(jclass c, jmethodID id, char rk, jvalue *a) {
    switch (rk) {
        case 'I': return (int64_t)(*g_env)->CallStaticIntMethodA(g_env, c, id, a);
        case 'J': return (int64_t)(*g_env)->CallStaticLongMethodA(g_env, c, id, a);
        case 'S': return (int64_t)(*g_env)->CallStaticShortMethodA(g_env, c, id, a);
        case 'B': return (int64_t)(*g_env)->CallStaticByteMethodA(g_env, c, id, a);
        case 'C': return (int64_t)(*g_env)->CallStaticCharMethodA(g_env, c, id, a);
        case 'Z': return (int64_t)(*g_env)->CallStaticBooleanMethodA(g_env, c, id, a);
        case 'D': return (int64_t)(*g_env)->CallStaticDoubleMethodA(g_env, c, id, a);
        case 'F': return (int64_t)(*g_env)->CallStaticFloatMethodA(g_env, c, id, a);
        default:  return 0;
    }
}

static double jni_static_f64(jclass c, jmethodID id, char rk, jvalue *a) {
    switch (rk) {
        case 'D': return (double)(*g_env)->CallStaticDoubleMethodA(g_env, c, id, a);
        case 'F': return (double)(*g_env)->CallStaticFloatMethodA(g_env, c, id, a);
        case 'I': return (double)(*g_env)->CallStaticIntMethodA(g_env, c, id, a);
        case 'J': return (double)(*g_env)->CallStaticLongMethodA(g_env, c, id, a);
        default:  return 0.0;
    }
}

static int64_t jni_method_i64(jobject o, jmethodID id, char rk, jvalue *a) {
    switch (rk) {
        case 'I': return (int64_t)(*g_env)->CallIntMethodA(g_env, o, id, a);
        case 'J': return (int64_t)(*g_env)->CallLongMethodA(g_env, o, id, a);
        case 'S': return (int64_t)(*g_env)->CallShortMethodA(g_env, o, id, a);
        case 'B': return (int64_t)(*g_env)->CallByteMethodA(g_env, o, id, a);
        case 'C': return (int64_t)(*g_env)->CallCharMethodA(g_env, o, id, a);
        case 'Z': return (int64_t)(*g_env)->CallBooleanMethodA(g_env, o, id, a);
        case 'D': return (int64_t)(*g_env)->CallDoubleMethodA(g_env, o, id, a);
        case 'F': return (int64_t)(*g_env)->CallFloatMethodA(g_env, o, id, a);
        default:  return 0;
    }
}

static double jni_method_f64(jobject o, jmethodID id, char rk, jvalue *a) {
    switch (rk) {
        case 'D': return (double)(*g_env)->CallDoubleMethodA(g_env, o, id, a);
        case 'F': return (double)(*g_env)->CallFloatMethodA(g_env, o, id, a);
        case 'I': return (double)(*g_env)->CallIntMethodA(g_env, o, id, a);
        case 'J': return (double)(*g_env)->CallLongMethodA(g_env, o, id, a);
        default:  return 0.0;
    }
}

static void jni_clear(void) {
    if ((*g_env)->ExceptionCheck(g_env)) (*g_env)->ExceptionClear(g_env);
}

int64_t fa_jvm_call_static_i64(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_static(cls, m, sig);
    if (!id) return 0;
    int64_t r = jni_static_i64((jclass)cls, id, jni_ret_kind(sig), (jvalue *)args);
    jni_clear();
    return r;
}

double fa_jvm_call_static_f64(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_static(cls, m, sig);
    if (!id) return 0.0;
    double r = jni_static_f64((jclass)cls, id, jni_ret_kind(sig), (jvalue *)args);
    jni_clear();
    return r;
}

void *fa_jvm_call_static_obj(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_static(cls, m, sig);
    if (!id) return NULL;
    char rk = jni_ret_kind(sig);
    if (rk != 'L' && rk != '[') return NULL;      /* 返回类型不是对象 */
    jvalue *jv = (jvalue *)args;
    jobject r = (*g_env)->CallStaticObjectMethodA(g_env, (jclass)cls, id, jv);
    if ((*g_env)->ExceptionCheck(g_env)) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return to_global(r);
}

void fa_jvm_call_static_void(void *cls, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_static(cls, m, sig);
    if (!id) return;
    jvalue *jv = (jvalue *)args;
    (*g_env)->CallStaticVoidMethodA(g_env, (jclass)cls, id, jv);
    if ((*g_env)->ExceptionCheck(g_env)) (*g_env)->ExceptionClear(g_env);
}

void *fa_jvm_new_obj(void *cls, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    if (!cls || !sig) return NULL;
    jmethodID id = (*g_env)->GetMethodID(g_env, (jclass)cls, "<init>", sig->data);
    if (!id) { (*g_env)->ExceptionClear(g_env); return NULL; }
    jvalue *jv = (jvalue *)args;
    jobject o = (*g_env)->NewObjectA(g_env, (jclass)cls, id, jv);
    if ((*g_env)->ExceptionCheck(g_env)) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return to_global(o);
}

/* ---- 实例方法调用 ---- */
static jmethodID get_method(void *obj, FaStr *m, FaStr *sig) {
    if (!obj || !m || !sig) return NULL;
    jclass c = (*g_env)->GetObjectClass(g_env, (jobject)obj);
    jmethodID id = (*g_env)->GetMethodID(g_env, c, m->data, sig->data);
    if (!id) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return id;
}

int64_t fa_jvm_call_i64(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_method(obj, m, sig);
    if (!id) return 0;
    int64_t r = jni_method_i64((jobject)obj, id, jni_ret_kind(sig), (jvalue *)args);
    jni_clear();
    return r;
}

double fa_jvm_call_f64(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_method(obj, m, sig);
    if (!id) return 0.0;
    double r = jni_method_f64((jobject)obj, id, jni_ret_kind(sig), (jvalue *)args);
    jni_clear();
    return r;
}

void *fa_jvm_call_obj(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_method(obj, m, sig);
    if (!id) return NULL;
    jobject r = (*g_env)->CallObjectMethodA(g_env, (jobject)obj, id, (jvalue *)args);
    if ((*g_env)->ExceptionCheck(g_env)) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return to_global(r);
}

void fa_jvm_call_void(void *obj, FaStr *m, FaStr *sig, int64_t n, uint64_t *args) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    jmethodID id = get_method(obj, m, sig);
    if (!id) return;
    (*g_env)->CallVoidMethodA(g_env, (jobject)obj, id, (jvalue *)args);
    if ((*g_env)->ExceptionCheck(g_env)) (*g_env)->ExceptionClear(g_env);
}

void *fa_jvm_str(FaStr *s) {
    if (!fa_jvm_ready) fa_jvm_init_default();
    if (!g_env) return NULL;
    jstring js = (*g_env)->NewStringUTF(g_env, s ? s->data : "");
    if (!js) { (*g_env)->ExceptionClear(g_env); return NULL; }
    return to_global((jobject)js);
}

FaStr *fa_jvm_to_str(void *obj) {
    if (!obj || !g_env) return fa_str_from_cstr("");
    jclass sc = (*g_env)->FindClass(g_env, "java/lang/String");
    jmethodID id = (*g_env)->GetStaticMethodID(g_env, sc, "valueOf",
                                               "(Ljava/lang/Object;)Ljava/lang/String;");
    if (!id) { (*g_env)->ExceptionClear(g_env); return fa_str_from_cstr(""); }
    jvalue jv[1]; jv[0].l = (jobject)obj;
    jobject s = (*g_env)->CallStaticObjectMethodA(g_env, sc, id, jv);
    if (!s) { (*g_env)->ExceptionClear(g_env); return fa_str_from_cstr(""); }
    const char *c = (*g_env)->GetStringUTFChars(g_env, (jstring)s, NULL);
    FaStr *r = fa_str_from_cstr(c ? c : "");
    if (c) (*g_env)->ReleaseStringUTFChars(g_env, (jstring)s, c);
    (*g_env)->DeleteLocalRef(g_env, s);
    return r;
}

int64_t fa_jvm_to_i64(void *obj) {
    FaStr *s = fa_jvm_to_str(obj);
    int64_t v = fa_str_to_i64(s);
    fa_free(s);
    return v;
}

double fa_jvm_to_f64(void *obj) {
    FaStr *s = fa_jvm_to_str(obj);
    double v = fa_str_to_f64(s);
    fa_free(s);
    return v;
}

/* 注意：不要在进程退出时调用 DestroyJavaVM。
 * 在嵌入式场景下，JVM 关闭路径会去加载类（ClassLoader::load_class），
 * 而此时进程状态已不适合运行 JVM，极易在 libjimage 中崩溃。
 * 进程退出会直接回收整个 JVM，显式销毁既没必要也不安全。 */
__attribute__((destructor)) static void fa_jvm_fini(void) {
    fa_flush();
}

#else

int64_t fa_jvm_init(FaStr *c) { (void)c; return 0; }
int64_t fa_jvm_init_default(void) { return 0; }
void *fa_jvm_find_class(FaStr *n) { (void)n; return NULL; }
int64_t fa_jvm_call_static_i64(void *c, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)c; (void)m; (void)s; (void)n; (void)a; return 0;
}
double fa_jvm_call_static_f64(void *c, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)c; (void)m; (void)s; (void)n; (void)a; return 0.0;
}
void *fa_jvm_call_static_obj(void *c, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)c; (void)m; (void)s; (void)n; (void)a; return NULL;
}
void fa_jvm_call_static_void(void *c, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)c; (void)m; (void)s; (void)n; (void)a;
}
void *fa_jvm_new_obj(void *c, FaStr *s, int64_t n, uint64_t *a) {
    (void)c; (void)s; (void)n; (void)a; return NULL;
}
int64_t fa_jvm_call_i64(void *o, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)o; (void)m; (void)s; (void)n; (void)a; return 0;
}
double fa_jvm_call_f64(void *o, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)o; (void)m; (void)s; (void)n; (void)a; return 0.0;
}
void *fa_jvm_call_obj(void *o, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)o; (void)m; (void)s; (void)n; (void)a; return NULL;
}
void fa_jvm_call_void(void *o, FaStr *m, FaStr *s, int64_t n, uint64_t *a) {
    (void)o; (void)m; (void)s; (void)n; (void)a;
}
void *fa_jvm_str(FaStr *s) { (void)s; return NULL; }
FaStr *fa_jvm_to_str(void *o) { (void)o; return fa_str_from_cstr("<java disabled>"); }
int64_t fa_jvm_to_i64(void *o) { (void)o; return 0; }
double fa_jvm_to_f64(void *o) { (void)o; return 0.0; }

#endif
