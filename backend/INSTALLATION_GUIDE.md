# Installation Guide

This sandbox package includes a compiled Linux candidate when PyInstaller succeeds and Windows installer scripts for local Windows build.

Windows compile:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\compile_pyinstaller.ps1
```

Windows installer build with Inno Setup installed:

```powershell
powershell -ExecutionPolicy Bypass -File .\installeruild_installer.ps1
```
