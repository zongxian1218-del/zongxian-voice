; 棕仙的传输软件 —— 自解压便携版（NSIS 3）
; 编译：makensis /INPUTCHARSET UTF8 /DAPPDIR=... /DOUTFILE=... sfx.nsi
;
; 为什么单独做这个：老安装包往 %LOCALAPPDATA%\Programs 覆盖安装时，
; 一旦有个文件被别的进程占着就会弹「无法打开要写入的文件」并中断整个安装。
; 自解压版换了个思路，从根上避开这件事：
;   1. 默认解压到**桌面**（用户自己的目录，不涉及安装目录权限）；
;   2. 目标目录已存在就自动换一个不冲突的名字（-新 / -新2 …），不去覆盖正在用的文件；
;   3. 所有拷贝都用 SetOverwrite try：个别文件被占用就跳过，**绝不中断**；
;   4. 不写系统目录、不需要管理员。

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"

!ifndef APPDIR
  !define APPDIR "..\dist\棕仙的传输软件"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\棕仙的传输软件-自解压版.exe"
!endif
!ifndef ICONFILE
  !define ICONFILE "..\dist\zongxian.ico"
!endif
!ifndef APPEXE
  !define APPEXE "棕仙的传输软件.exe"
!endif

!define APP_NAME "棕仙的传输软件"
!define APP_VER "1.0"
!define REG_KEY "Software\ZongxianTransfer"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZongxianTransfer"
!define RUN_VALUE "棕仙的传输软件"
!define UNINST_EXE "卸载-棕仙的传输软件.exe"

Name "${APP_NAME} ${APP_VER} 便携版"
OutFile "${OUTFILE}"
; 默认解压到桌面，用户也可以自己改
InstallDir "$DESKTOP\${APP_NAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show

!define MUI_ICON "${ICONFILE}"
!define MUI_UNICON "${ICONFILE}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "解压即用，不写系统目录"
!define MUI_WELCOMEPAGE_TEXT "这个版本会把「${APP_NAME}」解压到你选的目录（默认桌面），$\r$\n创建桌面和开始菜单快捷方式，然后可以直接运行。$\r$\n$\r$\n不需要管理员权限，也不会覆盖正在运行中的旧文件。"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPEXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\zongxian-transfer-guide.md"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "打开使用说明（推荐先看一眼跨网那节）"
!define MUI_FINISHPAGE_LINK "同目录下的 swiftdrop.html 是网页版，可以直接发给朋友"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; 目标目录里已经有程序（之前解压过）→ 自动换一个不冲突的目录名，
; 免得去覆盖正在运行的那个 exe。
Function .onInit
  IfFileExists "$INSTDIR\${APPEXE}" 0 done
    StrCpy $INSTDIR "$INSTDIR-新"
    IfFileExists "$INSTDIR\${APPEXE}" 0 done
      StrCpy $INSTDIR "$INSTDIR-2"
  done:
FunctionEnd

Section "解压文件" SecExtract
  ; 旧运行库目录尽量清掉（清不掉也没关系，后面 try 会跳过）
  RMDir /r "$INSTDIR\_internal"
  SetOutPath "$INSTDIR"
  SetOverwrite try
  File /r "${APPDIR}\*.*"
  ; 被占用的文件在最后再补一次（此时通常已释放）
  SetOverwrite try
  SetOutPath "$INSTDIR"
  File "${APPDIR}\${APPEXE}"
  File "${APPDIR}\swiftdrop.html"
  File "${APPDIR}\zongxian-transfer-guide.md"

  WriteUninstaller "$INSTDIR\${UNINST_EXE}"
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" "$INSTDIR\${UNINST_EXE}"

  WriteRegStr HKCU "${REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}（便携版）"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VER}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "棕仙"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\${UNINST_EXE}"'
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APPEXE}"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ExecWait 'taskkill /F /IM "${APPEXE}"' $0
  Sleep 600
  Delete "$DESKTOP\${APP_NAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${REG_KEY}"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${RUN_VALUE}"
  ; 只删自己解压出来的那个目录
  Delete "$INSTDIR\${UNINST_EXE}"
  RMDir /r "$INSTDIR"
SectionEnd
