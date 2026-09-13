// 纯头文件的 C++ 夹具：inline 函数 + STL + 类，全在头文件里，不需要 .so。
// 128_cxx_header_only.fa 用它验三件事：
//   1) `use cxx "头.hpp"` 不给 lib 也能编（shim 用 g++ 编，头里的实现直接进去）；
//   2) 链接要带 -lstdc++（用到 new/delete、STL 容器、异常机制的符号都在里头）；
//   3) FA 的 str 在边界上就是 const char*（不是 FaStr*）。
#pragma once
#include <string>
#include <vector>
#include <algorithm>
#include <numeric>

inline int vec_sum(const std::vector<int> &v) {
    return std::accumulate(v.begin(), v.end(), 0);
}

inline int sum_of_squares(int n) {
    std::vector<int> v;
    for (int i = 1; i <= n; i++) v.push_back(i * i);
    return vec_sum(v);
}

struct Counter {
    int n = 0;
    void bump(int by) { n += by; }
    int get() const { return n; }
};

inline int counter_demo(int a, int b) {
    Counter c;
    c.bump(a);
    c.bump(b);
    return c.get();
}

// 收字符串：FA 侧声明成 str，到这儿就是 const char*
inline int char_count(const char *s) {
    return (int)std::string(s).size();
}

// 返回字符串：得给 const char*（std::string 的临时对象出函数就没了），
// 所以用一个 static 缓冲把结果留住。这不是线程安全的写法，只是夹具够用。
inline const char *shout(const char *s) {
    static std::string buf;
    buf = std::string(s);
    std::transform(buf.begin(), buf.end(), buf.begin(), ::toupper);
    return buf.c_str();
}
