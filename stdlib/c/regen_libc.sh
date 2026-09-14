#!/bin/sh
# 重新生成 stdlib/c/libc.fa。
#
# libc.fa 是 `fa bind` 从本机系统头文件生成的**vendored 绑定**：它入库，是因为
# 标准库不应该要求使用者先装一遍工具链再生成一遍；它入库，也因为它随时可以用
# 这个脚本重新生成、并且有测试盯着（tests/run_bindgen.py 会现场绑同一批头文件，
# 断言该绑到的都绑到了、结构体布局与真编译器量出来的一致）。
#
# 要加一个函数：把名字加进 --only，然后跑这个脚本，再跑 tests/run_bindgen.py。
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
cd "$root"

./bin/fa bind dirent.h sys/stat.h unistd.h fcntl.h time.h regex.h stdlib.h stdio.h \
  --ptr-return strptime \
  --only opendir,readdir,closedir,dirent,stat,lstat,mkdir,chmod,access,unlink,rmdir,\
getcwd,open,write,close,lseek,rename,remove,time,clock_gettime,timespec,CLOCK_REALTIME,\
localtime_r,gmtime_r,strftime,strptime,mktime,tm,difftime,regcomp,regexec,regfree,regerror,regmatch_t,\
re_pattern_buffer,realpath,S_IFDIR,S_IFREG,S_IFLNK,S_IRUSR,S_IWUSR,S_IXUSR,\
O_RDONLY,O_WRONLY,O_RDWR,O_CREAT,O_TRUNC,O_APPEND,F_OK,R_OK,W_OK,X_OK,\
REG_EXTENDED,REG_ICASE,REG_NEWLINE,REG_NOSUB,REG_NOTBOL,REG_NOTEOL \
  -o stdlib/c/libc.fa
