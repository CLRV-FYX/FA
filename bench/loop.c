#include <stdio.h>
int main(void){
    long s=0,i=0;
    while(i<100000000){ s = s + i*3 - i/2; i++; }
    printf("%ld\n", s); return 0;
}
