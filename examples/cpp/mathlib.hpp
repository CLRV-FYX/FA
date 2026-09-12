#pragma once
#include <cstdint>

// 一个普通的 C++ 库（可以随意使用 class / 模板 / STL）
int      add_ints(int a, int b);
double   mean(const double* arr, int n);
int64_t  fact(int n);

template <typename T>
T twice(T v) { return v + v; }          // 模板：需要手写 shim，见下方
int twice_int(int v);                    // 模板的手工包装
