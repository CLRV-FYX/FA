#include "mathlib.hpp"
#include <numeric>

int add_ints(int a, int b) { return a + b; }

double mean(const double* arr, int n) {
    if (n <= 0) return 0.0;
    double s = 0.0;
    for (int i = 0; i < n; i++) s += arr[i];
    return s / n;
}

int64_t fact(int n) {
    int64_t r = 1;
    for (int i = 2; i <= n; i++) r *= i;
    return r;
}

int twice_int(int v) { return twice<int>(v); }
