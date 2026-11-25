; Hooks for the NSIS installer (tauri.installer.conf.json), and where the rest of it is for
; installer.nsi: this folder, and the pictures build-installer.ps1 stages.
!define ZC_WINDOWS "${__FILEDIR__}"
!define ZC_ART "${ZC_WINDOWS}\..\installer\art"

; A per-user install goes to %LOCALAPPDATA%\ZachCap by default, which is where ZachCap keeps
; its meetings and database. The program goes to %LOCALAPPDATA%\Programs\ZachCap instead, so
; the two stay apart. A folder chosen in the installer is kept as chosen.
!macro NSIS_HOOK_PREINSTALL
  ${If} $INSTDIR == "$LOCALAPPDATA\${PRODUCTNAME}"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${PRODUCTNAME}"
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

; Once ZachCap is closed, before anything is copied. An update doesn't uninstall the version
; that is there, so its wheel would stay beside the new one (the engine is set up from the new
; one only: wheel_for in lib.rs), and so would the source files this one no longer has, which
; the uninstaller wouldn't know of.
!macro ZC_HOOK_BEFORE_FILES
  ${If} ${FileExists} "$INSTDIR\engine\BUILD"
    Delete "$INSTDIR\engine\wheel\*.whl"
    RMDir /r "$INSTDIR\engine\src"
  ${EndIf}
!macroend

; The engine's venv is set up on first launch (a few GB) next to the program, so uninstalling
; removes it too, with what's left of one that was replaced. That happens after the uninstaller
; has closed ZachCap, and a moment later, so the engine (which exits with the app) isn't running
; from it; a venv left half removed is set up again on the next start (set_up in lib.rs). An
; update keeps it. Your meetings, voices and settings in %LOCALAPPDATA%\ZachCap are never
; touched.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    Sleep 3000
    RMDir /r "$INSTDIR\engine\.venv"
    RMDir /r "$INSTDIR\engine\.venv-old"
    ; The uninstaller tried these while the venv was still in them; only removed when empty.
    RMDir "$INSTDIR\engine"
    RMDir "$INSTDIR"
  ${EndIf}
!macroend
