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
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
