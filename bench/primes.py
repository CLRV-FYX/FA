def count_primes(n):
    f = bytearray(b'\x01')*n
    count = 0
    for p in range(2, n):
        if f[p]:
            count += 1
            f[p*p:n:p] = b'\x00'*(((n-1-p*p)//p)+1)
    return count
print(count_primes(2000000))
