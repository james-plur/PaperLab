const {app,BrowserWindow,dialog,ipcMain,shell,nativeTheme,Menu,safeStorage}=require("electron");
const {spawn}=require("child_process");
const crypto=require("crypto");
const fs=require("fs");
const path=require("path");

let mainWindow=null, backend=null, connection=null;
let pendingPaperId=null;
function paperIdFromArgs(argv){
  const direct=argv.find(value=>String(value).startsWith("paperlab://"));
  if(direct){try{const url=new URL(direct);if(url.hostname==="paper")return decodeURIComponent(url.pathname.replace(/^\//,""));}catch{}}
  const index=argv.indexOf("--paper-id");return index>=0&&argv[index+1]?String(argv[index+1]):null;
}
function openPaper(paperId){
  if(!paperId)return;pendingPaperId=paperId;
  if(mainWindow&&!mainWindow.webContents.isLoading()){mainWindow.webContents.send("paperlab:command",`open-paper:${paperId}`);pendingPaperId=null;}
}
const settingsPath=()=>path.join(app.getPath("userData"),"settings.json");
function readSettings(){try{return JSON.parse(fs.readFileSync(settingsPath(),"utf8"));}catch{return {};}}
function writeSettings(value){fs.mkdirSync(path.dirname(settingsPath()),{recursive:true});fs.writeFileSync(settingsPath(),JSON.stringify(value,null,2));}
function providerSecrets(){const stored=readSettings().providerSecrets||{},result={};for(const [name,value] of Object.entries(stored)){try{result[name]=safeStorage.decryptString(Buffer.from(value,"base64"));}catch{}}return result;}
function migrateLegacySecrets(config){if(!safeStorage.isEncryptionAvailable())return;const candidates=[path.join(path.dirname(config),"data/llm_config.json"),path.join(path.dirname(config),"llm_config.json")];for(const file of candidates){if(!fs.existsSync(file))continue;try{const value=JSON.parse(fs.readFileSync(file,"utf8")),settings=readSettings(),stored={...(settings.providerSecrets||{})};let changed=false;for(const name of ["siliconflow","deepseek","zhipu"]){const key=value.providers?.[name]?.api_key;if(key&&!stored[name]){stored[name]=safeStorage.encryptString(String(key)).toString("base64");delete value.providers[name].api_key;changed=true}}if(changed){writeSettings({...settings,providerSecrets:stored});const temp=file+".tmp";fs.writeFileSync(temp,JSON.stringify(value,null,2)+"\n",{mode:0o600});fs.renameSync(temp,file)}}catch(error){process.stderr.write(`[paperlab] legacy secret migration skipped: ${error}\n`)}}}
function configPath(){
  const settings=readSettings(); if(settings.configPath&&fs.existsSync(settings.configPath))return settings.configPath;
  const candidates=[path.join(app.getPath("home"),"tools/paperlab/topics.yaml"),path.join(app.getPath("home"),"paperlab/topics.yaml")];
  const found=candidates.find(fs.existsSync); if(found){writeSettings({...settings,configPath:found});return found;}
  return null;
}
async function chooseConfig(){
  let config=configPath(); if(config)return config;
  const choice=await dialog.showOpenDialog({title:"选择 PaperLab topics.yaml",properties:["openFile"],filters:[{name:"YAML",extensions:["yaml","yml"]}]});
  if(choice.canceled||!choice.filePaths[0])throw new Error("PaperLab configuration is required");
  config=choice.filePaths[0];writeSettings({...readSettings(),configPath:config});return config;
}
function backendCommand(){
  if(app.isPackaged)return {command:path.join(process.resourcesPath,"sidecar/paperlab-sidecar"),args:[]};
  const root=path.resolve(__dirname,"../..");
  return {command:path.join(root,".venv/bin/python"),args:["-m","paperlab_local.cli"]};
}
async function startBackend(){
  const config=await chooseConfig(), token=crypto.randomBytes(32).toString("base64url"), spec=backendCommand();
  migrateLegacySecrets(config);
  const args=[...spec.args,"--config",config,"app-server","--port","0","--app-token",token];
  const secrets=providerSecrets();
  backend=spawn(spec.command,args,{cwd:path.dirname(config),env:{...process.env,PAPERLAB_AUTO_SYNC:"0",PAPERLAB_LOCAL_TRUSTED_MODE:"0",SILICONFLOW_API_KEY:secrets.siliconflow||process.env.SILICONFLOW_API_KEY||"",DEEPSEEK_API_KEY:secrets.deepseek||process.env.DEEPSEEK_API_KEY||"",ZAI_API_KEY:secrets.zhipu||process.env.ZAI_API_KEY||""},stdio:["ignore","pipe","pipe"]});
  connection=await new Promise((resolve,reject)=>{
    let buffer="";const timer=setTimeout(()=>reject(new Error("PaperLab backend startup timed out")),120000);
    backend.stdout.on("data",chunk=>{buffer+=chunk.toString();const lines=buffer.split("\n");buffer=lines.pop();for(const line of lines){if(line.startsWith("PAPERLAB_READY ")){clearTimeout(timer);const ready=JSON.parse(line.slice(15));resolve({baseUrl:`http://${ready.host}:${ready.port}`,token,configPath:config});}}});
    backend.stderr.on("data",chunk=>process.stderr.write(`[paperlab] ${chunk}`));
    backend.once("exit",code=>{if(!connection){clearTimeout(timer);reject(new Error(`PaperLab backend exited (${code})`));}});
    backend.once("error",reject);
  });
  return connection;
}
function stopBackend(){if(!backend)return;backend.kill("SIGTERM");const proc=backend;backend=null;setTimeout(()=>{if(proc.exitCode===null)proc.kill("SIGKILL");},3000).unref();}
async function restartBackend(){stopBackend();connection=null;await new Promise(resolve=>setTimeout(resolve,350));const next=await startBackend();mainWindow?.webContents.send("paperlab:connection-changed",next);return next;}
async function createWindow(){
  await startBackend();const settings=readSettings();const bounds=settings.windowBounds||{width:1500,height:940};
  mainWindow=new BrowserWindow({title:"PaperLab",...bounds,minWidth:1100,minHeight:700,titleBarStyle:"hiddenInset",backgroundColor:nativeTheme.shouldUseDarkColors?"#17191d":"#f4f4f2",webPreferences:{preload:path.join(__dirname,"preload.cjs"),contextIsolation:true,nodeIntegration:false,sandbox:true}});
  mainWindow.on("close",()=>writeSettings({...readSettings(),windowBounds:mainWindow.getBounds()}));
  if(process.env.VITE_DEV_SERVER_URL)await mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);else if(!app.isPackaged)await mainWindow.loadURL("http://localhost:5173");else await mainWindow.loadFile(path.join(__dirname,"../renderer-dist/index.html"));
  if(pendingPaperId){const paperId=pendingPaperId;setTimeout(()=>openPaper(paperId),1200);}
}
pendingPaperId=paperIdFromArgs(process.argv);
if(!app.requestSingleInstanceLock())app.quit();else{
  app.setAsDefaultProtocolClient("paperlab");
  app.on("open-url",(event,url)=>{event.preventDefault();openPaper(paperIdFromArgs([url]));});
  app.on("second-instance",(_event,argv)=>{openPaper(paperIdFromArgs(argv));if(mainWindow){if(mainWindow.isMinimized())mainWindow.restore();mainWindow.focus();}});
  app.whenReady().then(()=>{Menu.setApplicationMenu(Menu.buildFromTemplate([{label:"PaperLab",submenu:[{role:"about"},{type:"separator"},{label:"Settings…",accelerator:"CmdOrCtrl+,",click:()=>mainWindow?.webContents.send("paperlab:command","settings")},{type:"separator"},{role:"quit"}]},{label:"Edit",submenu:[{role:"undo"},{role:"redo"},{type:"separator"},{role:"cut"},{role:"copy"},{role:"paste"},{role:"selectAll"}]},{label:"View",submenu:[{role:"reload"},{role:"toggleDevTools"},{type:"separator"},{role:"resetZoom"},{role:"zoomIn"},{role:"zoomOut"}]},{label:"Window",submenu:[{role:"minimize"},{role:"zoom"},{role:"front"}]}]));return createWindow();}).catch(error=>{dialog.showErrorBox("PaperLab could not start",String(error.stack||error));app.quit();});
  app.on("before-quit",stopBackend);app.on("window-all-closed",()=>app.quit());
}
ipcMain.handle("paperlab:connection",()=>connection);
ipcMain.handle("paperlab:choose-import",async(_event,kind)=>{const result=await dialog.showOpenDialog({properties:["openFile"],filters:kind==="pdf"?[{name:"PDF",extensions:["pdf"]}]:[{name:"BibTeX",extensions:["bib","bibtex","txt"]}]});if(result.canceled)return null;const file=result.filePaths[0],stat=fs.statSync(file);if(stat.size>50*1024*1024)throw new Error("File exceeds 50 MB");return {name:path.basename(file),data:fs.readFileSync(file).toString("base64")};});
ipcMain.handle("paperlab:show-in-folder",(_event,target)=>shell.showItemInFolder(target));
ipcMain.handle("paperlab:open-external",(_event,url)=>{const parsed=new URL(url);if(!["http:","https:"].includes(parsed.protocol))throw new Error("Unsupported URL");return shell.openExternal(url);});
async function notePath(paperId){if(!connection)throw new Error("PaperLab backend is unavailable");const response=await fetch(`${connection.baseUrl}/api/desktop/papers/${encodeURIComponent(paperId)}/note-path`,{headers:{Authorization:`Bearer ${connection.token}`}});if(!response.ok)throw new Error(`Unable to resolve note (${response.status})`);return (await response.json()).path;}
ipcMain.handle("paperlab:open-note",async(_event,paperId)=>{const target=await notePath(String(paperId));const error=await shell.openPath(target);if(error)throw new Error(error);return true;});
ipcMain.handle("paperlab:show-note",async(_event,paperId)=>{shell.showItemInFolder(await notePath(String(paperId)));return true;});
ipcMain.handle("paperlab:get-pref",(_event,key)=>readSettings().preferences?.[key]);
ipcMain.handle("paperlab:set-pref",(_event,key,value)=>{const settings=readSettings();settings.preferences={...(settings.preferences||{}),[key]:value};writeSettings(settings);});
ipcMain.handle("paperlab:secret-status",()=>{const secrets=providerSecrets();return Object.fromEntries(["siliconflow","deepseek","zhipu"].map(name=>[name,Boolean(secrets[name])]));});
ipcMain.handle("paperlab:set-secret",async(_event,name,value)=>{if(!["siliconflow","deepseek","zhipu"].includes(name))throw new Error("Unsupported provider");if(!safeStorage.isEncryptionAvailable())throw new Error("macOS secure storage is unavailable");const settings=readSettings(),stored={...(settings.providerSecrets||{})};if(value)stored[name]=safeStorage.encryptString(String(value)).toString("base64");else delete stored[name];writeSettings({...settings,providerSecrets:stored});await restartBackend();return true;});
