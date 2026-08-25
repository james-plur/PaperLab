const {contextBridge,ipcRenderer}=require("electron");
contextBridge.exposeInMainWorld("paperlab",{
  connection:()=>ipcRenderer.invoke("paperlab:connection"),
  chooseImport:kind=>ipcRenderer.invoke("paperlab:choose-import",kind),
  showInFolder:path=>ipcRenderer.invoke("paperlab:show-in-folder",path),
  openExternal:url=>ipcRenderer.invoke("paperlab:open-external",url),
  openNote:paperId=>ipcRenderer.invoke("paperlab:open-note",paperId),
  showNote:paperId=>ipcRenderer.invoke("paperlab:show-note",paperId),
  getPreference:key=>ipcRenderer.invoke("paperlab:get-pref",key),
  setPreference:(key,value)=>ipcRenderer.invoke("paperlab:set-pref",key,value),
  secretStatus:()=>ipcRenderer.invoke("paperlab:secret-status"),
  setSecret:(name,value)=>ipcRenderer.invoke("paperlab:set-secret",name,value),
  onCommand:callback=>{const handler=(_event,value)=>callback(value);ipcRenderer.on("paperlab:command",handler);return()=>ipcRenderer.removeListener("paperlab:command",handler)}
});
