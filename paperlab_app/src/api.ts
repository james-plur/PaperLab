export async function connect(){return window.paperlab.connection();}
export async function api<T>(path:string, options:RequestInit={}):Promise<T>{
  const c=await connect(); const headers=new Headers(options.headers); headers.set("Authorization",`Bearer ${c.token}`);
  if(options.body && !(options.body instanceof Blob) && !headers.has("Content-Type"))headers.set("Content-Type","application/json");
  const response=await fetch(c.baseUrl+path,{...options,headers});
  const data=await response.json().catch(()=>({error:`HTTP ${response.status}`}));
  if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`); return data as T;
}
export async function pdfSource(id:string){const c=await connect();return {url:`${c.baseUrl}/pdf/${encodeURIComponent(id)}`,httpHeaders:{Authorization:`Bearer ${c.token}`}};}
