[app]
title = Internet Archive Download Manager
package.name = iadownloadmanager
package.domain = org.iadm
source.dir = .
source.include_exts = py,png,jpg,kv,json
version = 1.0.0
requirements = python3,kivy==2.3.1,pyjnius,android,certifi,openssl
orientation = portrait, landscape
fullscreen = 0
icon.filename = %(source.dir)s/icon.png
presplash.filename = %(source.dir)s/icon.png
android.permissions = INTERNET
android.api = 34
android.minapi = 29
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
