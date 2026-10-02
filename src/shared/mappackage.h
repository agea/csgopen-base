// Shared constraints for downloaded Eclipse Recoil map packages.
#ifndef CPP_MAP_PACKAGE_HEADER
#define CPP_MAP_PACKAGE_HEADER

enum { MAP_PACKAGE_LIMIT = 512*1024*1024 };

inline bool mappackagename(const char *name, string &map)
{
    if(!name) return false;
    if(!strncmp(name, "maps/", 5) || !strncmp(name, "maps\\", 5)) name += 5;
    size_t len = strlen(name);
    if(!len || len > 80) return false;
    loopi(len) if(!((name[i] >= 'a' && name[i] <= 'z') || (name[i] >= 'A' && name[i] <= 'Z') ||
        (name[i] >= '0' && name[i] <= '9') || name[i] == '_' || name[i] == '-')) return false;
    copystring(map, name);
    return true;
}

inline uint mappackagecrc(stream *f)
{
    uchar buf[65536];
    uint crc = crc32(0, NULL, 0);
    int len;
    while((len = f->read(buf, sizeof(buf))) > 0) crc = crc32(crc, buf, len);
    f->seek(0, SEEK_SET);
    return crc;
}

#endif
