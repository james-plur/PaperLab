export type PaperFilters={page:number;query:string;venue:string;year:string;org:string;sort:string};
export function buildPaperQuery(filters:PaperFilters){
  return new URLSearchParams({page:String(filters.page),per_page:"100",q:filters.query,
    venue:filters.venue,year:filters.year,org:filters.org,sort:filters.sort}).toString();
}
