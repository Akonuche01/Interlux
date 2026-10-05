plugins {
    id("com.android.application")
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.keneristudios.interlux"
    compileSdk = 36
    ndkVersion = "28.2.13676358"

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // Android 15 devices with 16KB pages cannot mmap .so files directly
        // from the APK unless they are 16KB-aligned. Flutter aligns to 4KB, so
        // extract the libraries to disk at install time instead.
        packaging.jniLibs.useLegacyPackaging = true
        applicationId = "com.keneristudios.interlux"
        minSdk = 24
        // targetSdk stays 28 ON PURPOSE (same as Termux). Verified by bisection
        // on-device (Android 15, 2026-09-23): targetSdk 29..36 -> execv of any
        // file under filesDir fails with EACCES (silent, no avc on user builds),
        // which kills the pty shell, proot, node, python — everything. 28 works.
        // compileSdk stays modern; distribution is sideload, not Play.
        targetSdk = 28
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    // Our userland bundles real Linux trees (npm's node_modules, python's
    // stdlib) that contain underscore-prefixed dirs (__generated__,
    // __phello__). AGP's default ignoreAssetsPattern drops `<dir>_*`, which
    // would silently delete npm's sigstore bindings. Keep every other default
    // exclusion, drop only the underscore-dir rule.
    aaptOptions {
        ignoreAssetsPattern = "!.svn:!.git:!.ds_store:!*.scc:.*:!CVS:!thumbs.db:!picasa.ini:!*~"
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    lint {
        checkReleaseBuilds = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

// ---------------------------------------------------------------------------
// The APK's engine copy is GENERATED, never hand-maintained.
//
// `src/main/assets/userland/agent/` used to be a hand-made copy of `agent/`,
// pruned by hand. It rotted: by 2026-10-02 it was missing two modules the
// engine imports (`calls/`, `home.py`) and it carried stale `*.bak` files and
// `__pycache__` directories. Nobody could tell which copy was authoritative,
// and a fix applied to one copy silently reverted on the next build.
//
// `agent/` is now the single source of truth -- folder and repo, nothing else.
// This task regenerates the asset from it on every build. `Sync` rather than
// `Copy` on purpose: it DELETES anything in the destination that is not in the
// source, which is what clears the accumulated backups and bytecode caches.
// ---------------------------------------------------------------------------
val syncUserlandAgent by tasks.registering(Sync::class) {
    from(file("../../agent")) {
        // Present in the source tree, never in the shipped APK.
        exclude("test_*.py")
        exclude("*.bak*")
        exclude("__pycache__/**")
        exclude("**/__pycache__/**")
        exclude("*.pyc")
    }
    into(layout.projectDirectory.dir("src/main/assets/userland/agent"))
}

// Any asset merge must run after the copy has been regenerated.
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }
    .configureEach { dependsOn(syncUserlandAgent) }
