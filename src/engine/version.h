#define STR_MACRO_HELPER(s) #s
#define STR_MACRO(s) STR_MACRO_HELPER(s)

#ifdef __clang__
  #define VERSION_COMP "clang-" \
    STR_MACRO(__clang_major__) "." \
    STR_MACRO(__clang_minor__) "." \
    STR_MACRO(__clang_patchlevel__)
#elif defined(_MSC_VER)
  #define VERSION_COMP "msvc-" STR_MACRO(_MSC_VER)
#else
  #define VERSION_COMP "gnuc-" \
    STR_MACRO(__GNUC__) "." \
    STR_MACRO(__GNUC_MINOR__) "." \
    STR_MACRO(__GNUC_PATCHLEVEL__)
#endif

#define VERSION_MAJOR 2
#define VERSION_MINOR 0
#define VERSION_PATCH 9
#define VERSION_HLP(x,y,z,r) #x#r#y#r#z
#define VERSION_STR(x,y,z,r) VERSION_HLP(x,y,z,r)
#define VERSION_STRING VERSION_STR(VERSION_MAJOR,VERSION_MINOR,VERSION_PATCH,.)
#define VERSION_NAME "Eclipse Recoil"
#define VERSION_FNAME "Eclipse Recoil"
#define VERSION_UNAME "eclipse-recoil"
#define VERSION_VNAME "ECLIPSE_RECOIL"
#define VERSION_RELEASE "Development"
#define VERSION_URL "github.com/agea/eclipse-recoil"
#define VERSION_COPY "2026"
#define VERSION_DESC "An arena shooter for the modern era"
#define VERSION_STEAM_APPID 0
#define VERSION_STEAM_DEPOT 0
#define VERSION_DISCORD ""

#ifndef VERSION_BUILD
#define VERSION_BUILD 0
#endif
#ifndef VERSION_BRANCH
#define VERSION_BRANCH "selfbuilt"
#endif
#ifndef VERSION_REVISION
#define VERSION_REVISION ""
#endif

#define LAN_PORT 28799
#define MASTER_PORT 28800
#define SERVER_PORT 28801
