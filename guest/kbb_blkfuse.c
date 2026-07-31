/*
 * kbb_blkfuse -- expose block devices as correctly-sized regular files.
 *
 * WHY THIS EXISTS
 * ---------------
 * Mode C hands the guest each ZIM slice as a file-backed virtio-scsi disk, so
 * the archive reaches the guest zero-copy with no admin and no raw-disk block.
 * The slice bytes are fully readable from the block device -- but libzim (and
 * therefore kiwix-serve) cannot open a block device: it sizes the file with
 * fstat(), and a block device reports st_size == 0. libzim then fails with
 * "zim-file is too small to contain a header" while the data sits right there.
 * Proven directly in the guest:
 *
 *     stat  st_size /dev/sda -> 0
 *     blockdev --getsize64   -> 1992294400   (the real size, only via ioctl)
 *     dd /dev/sda | od       -> ZIM \004 ...  (the header is readable)
 *
 * This filesystem bridges the gap. It presents a directory of regular files,
 * one per manifest line, each reporting the true size (BLKGETSIZE64) and
 * serving reads straight from its backing block device. libzim then stats a
 * real size, mmaps it, and follows the .zimaa/.zimab split sequence exactly as
 * it would for files on disk. Nothing is copied.
 *
 * The manifest is one "<name> <device>" per line, e.g.
 *     wikipedia_en_all_nopic_2026-06.zimaa /dev/sda
 *     wikipedia_en_all_nopic_2026-06.zimab /dev/sdb
 *
 * Read-only by construction: no create/write/unlink/truncate is implemented, so
 * the archive cannot be altered from inside the sandbox.
 *
 * Build: cc -O2 -Wall kbb_blkfuse.c -o kbb-blkfuse `pkg-config fuse3 --cflags --libs`
 * Run:   kbb-blkfuse <mountpoint> <manifest> [-f]
 */

#define FUSE_USE_VERSION 31

#include <fuse3/fuse.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/fs.h>

#define KBB_MAX_ENTRIES 512

struct kbb_entry {
    char name[256];   /* the .zimaa/.zimab filename the guest presents  */
    char dev[256];    /* the backing block device, e.g. /dev/sda         */
    off_t size;       /* real size via BLKGETSIZE64, resolved at startup */
};

static struct kbb_entry g_entries[KBB_MAX_ENTRIES];
static int g_count = 0;

/* Resolve a block device's true size. This is the whole point: fstat() returns
 * 0 for a block device, so we must ask the driver via ioctl. */
static off_t kbb_dev_size(const char *dev)
{
    int fd = open(dev, O_RDONLY);
    if (fd < 0)
        return -1;
    unsigned long long bytes = 0;
    off_t size = -1;
    if (ioctl(fd, BLKGETSIZE64, &bytes) == 0)
        size = (off_t)bytes;
    close(fd);
    return size;
}

static struct kbb_entry *kbb_lookup(const char *path)
{
    if (path[0] != '/')
        return NULL;
    for (int i = 0; i < g_count; i++)
        if (strcmp(path + 1, g_entries[i].name) == 0)
            return &g_entries[i];
    return NULL;
}

static int kbb_getattr(const char *path, struct stat *st,
                       struct fuse_file_info *fi)
{
    (void)fi;
    memset(st, 0, sizeof(*st));
    if (strcmp(path, "/") == 0) {
        st->st_mode = S_IFDIR | 0555;
        st->st_nlink = 2;
        return 0;
    }
    struct kbb_entry *e = kbb_lookup(path);
    if (!e)
        return -ENOENT;
    /* A regular file with the device's real size -- the fix in one line. */
    st->st_mode = S_IFREG | 0444;
    st->st_nlink = 1;
    st->st_size = e->size;
    return 0;
}

static int kbb_readdir(const char *path, void *buf, fuse_fill_dir_t filler,
                       off_t offset, struct fuse_file_info *fi,
                       enum fuse_readdir_flags flags)
{
    (void)offset; (void)fi; (void)flags;
    if (strcmp(path, "/") != 0)
        return -ENOENT;
    filler(buf, ".", NULL, 0, 0);
    filler(buf, "..", NULL, 0, 0);
    for (int i = 0; i < g_count; i++)
        filler(buf, g_entries[i].name, NULL, 0, 0);
    return 0;
}

static int kbb_open(const char *path, struct fuse_file_info *fi)
{
    struct kbb_entry *e = kbb_lookup(path);
    if (!e)
        return -ENOENT;
    /* Read-only: reject any write intent rather than silently accepting it. */
    if ((fi->flags & O_ACCMODE) != O_RDONLY)
        return -EACCES;
    return 0;
}

static int kbb_read(const char *path, char *buf, size_t size, off_t offset,
                    struct fuse_file_info *fi)
{
    (void)fi;
    struct kbb_entry *e = kbb_lookup(path);
    if (!e)
        return -ENOENT;
    int fd = open(e->dev, O_RDONLY);
    if (fd < 0)
        return -errno;
    /* pread so concurrent readers (kiwix serves many requests) never race a
     * shared file offset. */
    ssize_t n = pread(fd, buf, size, offset);
    int err = errno;
    close(fd);
    if (n < 0)
        return -err;
    return (int)n;
}

static const struct fuse_operations kbb_ops = {
    .getattr = kbb_getattr,
    .readdir = kbb_readdir,
    .open    = kbb_open,
    .read    = kbb_read,
};

/* Parse "<name> <device>" lines, resolving each device's real size up front so
 * getattr is a pure lookup. A device that cannot be sized is skipped with a
 * warning rather than aborting the whole mount -- one bad slice must not hide
 * every good one. */
static int kbb_load_manifest(const char *manifest)
{
    FILE *f = fopen(manifest, "r");
    if (!f) {
        fprintf(stderr, "kbb-blkfuse: cannot open manifest %s\n", manifest);
        return -1;
    }
    char line[600];
    while (fgets(line, sizeof(line), f) && g_count < KBB_MAX_ENTRIES) {
        char name[256], dev[256];
        if (sscanf(line, "%255s %255s", name, dev) != 2)
            continue;
        if (name[0] == '#')
            continue;
        off_t size = kbb_dev_size(dev);
        if (size <= 0) {
            fprintf(stderr, "kbb-blkfuse: skipping %s (%s: no size)\n", name, dev);
            continue;
        }
        struct kbb_entry *e = &g_entries[g_count++];
        snprintf(e->name, sizeof(e->name), "%s", name);
        snprintf(e->dev, sizeof(e->dev), "%s", dev);
        e->size = size;
        fprintf(stderr, "kbb-blkfuse: %s -> %s (%lld bytes)\n",
                name, dev, (long long)size);
    }
    fclose(f);
    return g_count;
}

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <mountpoint> <manifest> [fuse-opts...]\n",
                argv[0]);
        return 2;
    }
    const char *mountpoint = argv[1];
    const char *manifest = argv[2];

    if (kbb_load_manifest(manifest) <= 0) {
        fprintf(stderr, "kbb-blkfuse: no usable entries; nothing to mount\n");
        return 1;
    }

    /* Hand FUSE its own argv: program name, the mountpoint, and whatever fuse
     * options followed the manifest (e.g. -f to stay in the foreground, -o
     * allow_other so kiwix under a different context can read the files). */
    int fargc = 0;
    char *fargv[16];
    fargv[fargc++] = argv[0];
    fargv[fargc++] = (char *)mountpoint;
    for (int i = 3; i < argc && fargc < 15; i++)
        fargv[fargc++] = argv[i];

    return fuse_main(fargc, fargv, &kbb_ops, NULL);
}
