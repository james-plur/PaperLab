export {};
declare module "*?url" { const value: string; export default value; }
declare global {
  interface Window {
    paperlab: {
      connection(): Promise<{baseUrl:string; token:string; configPath:string}>;
      chooseImport(kind:"pdf"|"bibtex"): Promise<{name:string; data:string}|null>;
      showInFolder(path:string): Promise<void>;
      openExternal(url:string): Promise<void>;
      openNote(paperId:string): Promise<boolean>;
      showNote(paperId:string): Promise<boolean>;
      getPreference(key:string): Promise<unknown>;
      setPreference(key:string,value:unknown): Promise<void>;
      secretStatus(): Promise<Record<string,boolean>>;
      setSecret(name:string,value:string): Promise<boolean>;
      onCommand(callback:(command:string)=>void): ()=>void;
    };
  }
}
