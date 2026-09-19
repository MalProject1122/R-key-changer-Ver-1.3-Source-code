/* PC全体のメモリ使用率・CPU使用率を取得する薄いラッパー。
 *
 * CPU使用率は瞬間値を取れない（Windowsは累積時間しか返さない）ため、
 * 2回のサンプル間の差分から計算する。呼び出し側（Python）が
 * sysinfo_create() で状態を確保し、ポップアップを開いている間
 * 一定間隔で sysinfo_get_cpu_percent() を呼び続ける想定。
 */
#include <windows.h>
#include <psapi.h>
#include <stdlib.h>  /* calloc / free。windows.h 経由で通る環境もあるが明示する */

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

typedef struct {
    ULARGE_INTEGER prev_idle;
    ULARGE_INTEGER prev_kernel;
    ULARGE_INTEGER prev_user;
    int has_prev;
} SysInfoState;

static ULARGE_INTEGER to_uint64(FILETIME ft) {
    ULARGE_INTEGER v;
    v.LowPart = ft.dwLowDateTime;
    v.HighPart = ft.dwHighDateTime;
    return v;
}

EXPORT SysInfoState *sysinfo_create(void) {
    SysInfoState *st = (SysInfoState *)calloc(1, sizeof(SysInfoState));
    return st;
}

EXPORT void sysinfo_destroy(SysInfoState *st) {
    free(st);
}

/* GlobalMemoryStatusEx はシステム全体の物理メモリを返す
 * （このアプリのプロセス単体ではなくPC全体、という仕様どおり）。 */
EXPORT int sysinfo_get_memory(unsigned long long *total_bytes, unsigned long long *used_bytes) {
    MEMORYSTATUSEX status;
    status.dwLength = sizeof(status);
    if (!GlobalMemoryStatusEx(&status)) {
        return 0;
    }
    *total_bytes = status.ullTotalPhys;
    *used_bytes = status.ullTotalPhys - status.ullAvailPhys;
    return 1;
}

/* Windows の GetSystemTimes が返す kernelTime にはアイドル時間も
 * 含まれる仕様のため、busy = (kernel + user) - idle で計算する。
 * 初回呼び出しは比較対象が無いので -1.0（未計測）を返す。 */
EXPORT double sysinfo_get_cpu_percent(SysInfoState *st) {
    FILETIME idle_ft, kernel_ft, user_ft;
    if (!st || !GetSystemTimes(&idle_ft, &kernel_ft, &user_ft)) {
        return -1.0;
    }
    ULARGE_INTEGER idle = to_uint64(idle_ft);
    ULARGE_INTEGER kernel = to_uint64(kernel_ft);
    ULARGE_INTEGER user = to_uint64(user_ft);

    if (!st->has_prev) {
        st->prev_idle = idle;
        st->prev_kernel = kernel;
        st->prev_user = user;
        st->has_prev = 1;
        return -1.0;
    }

    unsigned long long delta_idle = idle.QuadPart - st->prev_idle.QuadPart;
    unsigned long long delta_kernel = kernel.QuadPart - st->prev_kernel.QuadPart;
    unsigned long long delta_user = user.QuadPart - st->prev_user.QuadPart;
    unsigned long long delta_total = delta_kernel + delta_user;

    st->prev_idle = idle;
    st->prev_kernel = kernel;
    st->prev_user = user;

    if (delta_total == 0) {
        return 0.0;
    }
    double busy = (double)(delta_total - delta_idle);
    double percent = 100.0 * busy / (double)delta_total;
    if (percent < 0.0) percent = 0.0;
    if (percent > 100.0) percent = 100.0;
    return percent;
}
