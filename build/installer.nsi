; 棕仙的传输软件 —— Windows 安装包脚本（NSIS 3）
; 编译：makensis /INPUTCHARSET UTF8 /DAPPDIR=... /DOUTFILE=... installer.nsi
; 特点：每用户安装（不需要管理员/UAC）、开始菜单+桌面快捷方式、
;       控制面板"程序和功能"里可卸载、可选开机自动同步、卸载时清理自启项。

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!ifndef APPDIR
  !define APPDIR "..\dist\棕仙的传输软件"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\棕仙的传输软件-安装包-1.0.exe"
!endif
!ifndef ICONFILE
  !define ICONFILE "..\dist\zongxian.ico"
!endif
!ifndef APPEXE
  !define APPEXE "棕仙的传输软件.exe"
!endif

; 调试用：定义 TRACE 后会把每一步写进 TRACEFILE，便于定位安装卡在哪
!ifdef TRACE
  !ifndef TRACEFILE
    !define TRACEFILE "trace.txt"
  !endif
  !macro TraceLine msg
    FileOpen $9 "${TRACEFILE}" a
    FileWrite $9 "${msg}$\r$\n"
    FileClose $9
  !macroend
!else
  !macro TraceLine msg
  !macroend
!endif

!define APP_NAME "棕仙的传输软件"
!define APP_VER "1.0"
!define APP_PUBLISHER "棕仙"
!define REG_KEY "Software\ZongxianTransfer"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZongxianTransfer"
!define RUN_VALUE "棕仙的传输软件"

Name "${APP_NAME} ${APP_VER}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${REG_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32

VIProductVersion "1.0.0.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "1.0.0.0"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VER}"
VIAddVersionKey /LANG=2052 "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "© ${APP_PUBLISHER}"

!define MUI_ICON "${ICONFILE}"
!define MUI_UNICON "${ICONFILE}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "欢迎安装 ${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT "这个程序会让你和朋友之间直接传大文件、自动同步文件夹：$\r$\n$\r$\n  · 不限速、不要公网 IP、不用注册$\r$\n  · 同一个 WiFi 里走局域网，速度跑满带宽$\r$\n  · 局域网里手机用浏览器就能传$\r$\n  · 支持断点续传、SHA-256 校验、文件夹双向同步$\r$\n$\r$\n点“下一步”继续。"
!define MUI_FINISHPAGE_TITLE "${APP_NAME} 安装完成"
!define MUI_FINISHPAGE_TEXT "已经装好了。第一次使用建议：打开软件 → 选一个要同步的文件夹 → 勾选“开机自动同步”。$\r$\n$\r$\n卸载可以在 Windows 的“设置 → 应用”里找到本程序。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPEXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"
!define MUI_DIRECTORYPAGE_TEXT_TOP "选择安装位置（默认装在当前用户目录下，不需要管理员权限）。"
!define MUI_ABORTWARNING_TEXT "确定要取消安装吗？"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

LangString DESC_Main ${LANG_SIMPCHINESE} "主程序和运行库（必需）。"
LangString DESC_Desktop ${LANG_SIMPCHINESE} "在桌面上放一个快捷方式。"
LangString DESC_Auto ${LANG_SIMPCHINESE} "开机后自动在后台同步你登记过的文件夹（可随时在软件里关掉；只在同一个局域网内有效）。"

Section "${APP_NAME}（必需）" SecMain
  SectionIn RO
  !insertmacro TraceLine "[install] SecMain 开始"
  ; 先结束正在运行的实例：否则 exe / python 运行库被占用，覆盖时会报
  ; 「无法打开要写入的文件」（Error opening file for writing）。
  ExecWait 'taskkill /F /IM "${APPEXE}"' $0
  Sleep 1200
  ; 内置网页版窗口 / 浏览器可能正开着 swiftdrop.html（服务端会持有文件句柄），
  ; 那会让下面的 File /r 报「无法打开要写入的文件」。这里先单独尝试删掉它，
  ; 删不掉也不中断安装（Delete /REBOOTOK 会安排重启后删除）。
  IfFileExists "$INSTDIR\swiftdrop.html" 0 +2
    Delete /REBOOTOK "$INSTDIR\swiftdrop.html"
  IfFileExists "$INSTDIR\zongxian-transfer-guide.md" 0 +2
    Delete /REBOOTOK "$INSTDIR\zongxian-transfer-guide.md"
  SetOutPath "$INSTDIR"
  ; try：个别文件被占用时跳过并继续，**不要整个安装中断**（旧行为是直接报错让用户 Abort）
  SetOverwrite try
  ; 旧版本的运行库目录先清掉，避免残留旧文件占用/混杂
  RMDir /r "$INSTDIR\_internal"
  File /r "${APPDIR}\*.*"
  ; 装完把被占用的文件补一次（前面跳过的这里再试，通常已经释放）
  SetOverwrite try
  SetOutPath "$INSTDIR"
  File "${APPDIR}\swiftdrop.html"
  File "${APPDIR}\zongxian-transfer-guide.md"
  !insertmacro TraceLine "[install] 文件已复制"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  !insertmacro TraceLine "[install] 卸载器已写入"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\使用说明.lnk" "$INSTDIR\zongxian-transfer-guide.md"
  !insertmacro TraceLine "[install] 开始菜单快捷方式完成 -> $SMPROGRAMS\${APP_NAME}"

  WriteRegStr HKCU "${REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REG_KEY}" "Version" "${APP_VER}"

  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APPEXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VER}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
  !insertmacro TraceLine "[install] 卸载注册表项已写入 HKCU\${UNINST_KEY}"
SectionEnd

Section "创建桌面快捷方式" SecDesktop
  !insertmacro TraceLine "[install] SecDesktop 开始"
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  !insertmacro TraceLine "[install] 桌面快捷方式 -> $DESKTOP\${APP_NAME}.lnk"
SectionEnd

; /o = 默认不勾选：避免装完就开机自启后台进程、反而让下次安装因文件占用失败
Section /o "开机自动同步文件夹" SecAuto
  !insertmacro TraceLine "[install] SecAuto 开始"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${RUN_VALUE}" '"$INSTDIR\${APPEXE}" autosync --hidden'
  !insertmacro TraceLine "[install] 开机自启项已写入"
  ; 自检：回读自己刚写的东西（定位"写了但看不到"这类问题）
  ReadRegStr $1 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${RUN_VALUE}"
  !insertmacro TraceLine "[verify] Run 回读=[$1]"
  ReadRegStr $2 HKCU "${UNINST_KEY}" "DisplayName"
  !insertmacro TraceLine "[verify] ARP.DisplayName 回读=[$2]"
  ReadRegStr $3 HKCU "${REG_KEY}" "InstallDir"
  !insertmacro TraceLine "[verify] InstallDir 回读=[$3]"
  IfFileExists "$DESKTOP\${APP_NAME}.lnk" 0 +2
  !insertmacro TraceLine "[verify] 桌面快捷方式：已存在"
  IfFileExists "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" 0 +2
  !insertmacro TraceLine "[verify] 开始菜单快捷方式：已存在"
  !insertmacro TraceLine "[verify] INSTDIR=[$INSTDIR] DESKTOP=[$DESKTOP] SMPROGRAMS=[$SMPROGRAMS]"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} $(DESC_Main)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_Desktop)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAuto} $(DESC_Auto)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
  !insertmacro TraceLine "[install] onInit 进入，InstallDir=$INSTDIR"
  ; 已经装过就先卸载旧版本，保证升级干净
  ReadRegStr $R0 HKCU "${UNINST_KEY}" "UninstallString"
  !insertmacro TraceLine "[install] 旧版卸载串=$R0"
  StrCmp $R0 "" done
  IfSilent uninst
  MessageBox MB_OKCANCEL|MB_ICONQUESTION "${APP_NAME} 已经装过了。$\r$\n$\r$\n点“确定”会先卸载旧版本（你的同步设置和文件夹标记会保留），再装新的。" IDOK uninst
  Abort
uninst:
  ClearErrors
  ExecWait '$R0 /S _?=$INSTDIR'
done:
  !insertmacro TraceLine "[install] onInit 结束"
FunctionEnd

Section "Uninstall"
  ; 关掉正在运行的程序和后台同步
  ExecWait 'taskkill /F /IM "${APPEXE}"'
  Sleep 400

  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"

  RMDir /r "$INSTDIR"

  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${RUN_VALUE}"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${REG_KEY}"
SectionEnd
