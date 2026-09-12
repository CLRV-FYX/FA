#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static long count_primes(long n){
    char *f = (char*)malloc((size_t)n); memset(f,1,(size_t)n);
    long count=0;
    for(long p=2;p<n;p++) if(f[p]){ count++; for(long k=p*p;k<n;k+=p) f[k]=0; }
    free(f); return count;
}
int main(void){ printf("%ld\n", count_primes(2000000)); return 0; }
