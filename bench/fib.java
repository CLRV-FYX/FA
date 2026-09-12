public class fib {
  static long f(long n){ return n<2?n:f(n-1)+f(n-2); }
  public static void main(String[] a){ System.out.println(f(38)); }
}
