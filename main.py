from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import datetime
import json
import logging
from urllib.parse import urlencode, quote

# 設定日誌記錄
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 建立 FastAPI 應用程式
app = FastAPI(
    title="政府採購網爬蟲 API",
    description="提供政府採購網資料爬取服務",
    version="1.0.0"
)

class ScrapeRequest(BaseModel):
    search_terms: List[str] = ["系統", "平台", "建置", "維運"]
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    page_size: int = 100

class ScrapeResponse(BaseModel):
    success: bool
    data: List[Dict]
    count: int
    message: str
    timestamp: str

class PCCWebScraper:
    """政府採購網爬蟲類別 - 不需要 Selenium 的版本"""
    
    def __init__(self):
        self.base_url = "https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic"
        self.session = None
        
    async def init_session(self):
        if not self.session:
            connector = aiohttp.TCPConnector(limit=10, ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive'
                }
            )
    
    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    async def scrape_by_keyword(self, keyword: str, start_date: str, end_date: str, page_size: int = 100) -> List[Dict]:
        if not self.session:
            await self.init_session()
            
        params = {
            'pageSize': page_size,
            'tenderStartDate': start_date,
            'tenderEndDate': end_date,
            'tenderName': keyword,
            'dateType': 'isDate'
        }
        query_string = urlencode(params, quote_via=quote)
        full_url = f"{self.base_url}?{query_string}"
        
        logger.info(f"正在爬取關鍵字: {keyword}")
        logger.info(f"請求網址: {full_url}")
        
        try:
            async with self.session.get(full_url) as response:
                if response.status != 200:
                    logger.error(f"請求失敗: {response.status}")
                    return []
                html_content = await response.text()
                return self.parse_html_content(html_content, keyword)
        except Exception as e:
            logger.error(f"爬取關鍵字 {keyword} 時發生錯誤: {str(e)}")
            return []
    
    def parse_html_content(self, html: str, keyword: str) -> List[Dict]:
        """解析網頁內容，分開案號跟名稱"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        table = soup.find('table', {'id': 'tpam'}) or soup.find('table', {'class': 'tb_01'})
        if not table:
            logger.warning(f"關鍵字 {keyword} 未找到資料表格")
            return []
        
        rows = table.find_all('tr')
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        for row in rows[1:]:
            cols = row.find_all('td')
            if len(cols) < 9:
                continue
            try:
                # 建立基本資料字典
                row_data = [col.get_text("\n", strip=True) for col in cols[:9]]
                keys = ["項次", "機關名稱", "標案案號&編號名稱", "傳輸次數", "招標方式", "採購性質", "公告日期", "截止投標", "預算金額"]
                row_dict = dict(zip(keys, row_data))

                # 取得連結
                href_url = ""
                a_tag = cols[2].find("a")
                if a_tag and a_tag.get('href') and 'pk=' in a_tag.get('href'):
                    pk_val = a_tag['href'].split('pk=')[-1].split('&')[0]
                    href_url = f"https://web.pcc.gov.tw/tps/QueryTender/query/searchTenderDetail?pkPmsMain={pk_val}"
                
                # 處理標案名稱和編號
                td = cols[2]
                # 標案名稱抓 <a> 文字
                a_tag = td.find("a")
                名稱 = ""
                if a_tag:
                    span_tag = a_tag.find("span") 
                    名稱 = span_tag.get_text(strip=True) if span_tag else a_tag.get_text(strip=True)

                # 標案編號抓 <td> 內剩下的文字
                編號_parts = [text for text in td.strings if text.strip() and text != 名稱]
                編號 = 編號_parts[0].strip() if 編號_parts else ""
                編號 = 編號.replace("(更正公告)", "").strip()
                    
                final_data = {
                    "項次": row_dict.get("項次", ""),
                    "機關名稱": row_dict.get("機關名稱", ""),
                    "標案編號": 編號,
                    "標案名稱": 名稱,
                    "傳輸次數": row_dict.get("傳輸次數", ""),
                    "招標方式": row_dict.get("招標方式", ""),
                    "採購性質": row_dict.get("採購性質", ""),
                    "公告日期": row_dict.get("公告日期", ""),
                    "截止投標": row_dict.get("截止投標", ""),
                    "預算金額": row_dict.get("預算金額", ""),
                    "網址": href_url,
                    "爬取日期": today,
                    "關鍵字": keyword
                }

                if 名稱 and row_dict.get("機關名稱"):
                    results.append(final_data)

            except Exception as e:
                logger.error(f"解析行資料時發生錯誤: {str(e)}")
                continue

        logger.info(f"關鍵字 {keyword} 獲得 {len(results)} 筆資料")
        return results
    
    async def scrape_multiple_keywords(self, keywords: List[str], start_date: str, end_date: str, page_size: int = 100) -> List[Dict]:
        all_results = []
        for keyword in keywords:
            if all_results:
                await asyncio.sleep(2)
            keyword_results = await self.scrape_by_keyword(keyword, start_date, end_date, page_size)
            all_results.extend(keyword_results)
        return all_results

scraper = PCCWebScraper()

@app.on_event("startup")
async def startup_event():
    await scraper.init_session()
    logger.info("政府採購網爬蟲 API 服務已啟動")

@app.on_event("shutdown")
async def shutdown_event():
    await scraper.close_session()
    logger.info("政府採購網爬蟲 API 服務已關閉")

@app.get("/")
async def root():
    return {
        "service": "政府採購網爬蟲 API",
        "version": "1.0.0",
        "author": "Nick Chang｜nickleo051216@gmail.com｜0932-684-051",
        "website": "ZN Studio｜https://portaly.cc/zn.studio",
        "status": "running",
        "timestamp": datetime.datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now().isoformat(),
        "service": "政府採購網爬蟲"
    }

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tenders(request: ScrapeRequest):
    if not request.start_date:
        request.start_date = datetime.date.today().strftime('%Y/%m/%d')
    if not request.end_date:
        request.end_date = datetime.date.today().strftime('%Y/%m/%d')
    
    results = await scraper.scrape_multiple_keywords(
        keywords=request.search_terms,
        start_date=request.start_date,
        end_date=request.end_date,
        page_size=request.page_size
    )
    
    n8n_format_results = [{"json": item} for item in results]
    return ScrapeResponse(
        success=True,
        data=n8n_format_results,
        count=len(results),
        message=f"成功爬取 {len(results)} 筆資料",
        timestamp=datetime.datetime.now().isoformat()
    )

@app.post("/scrape-today")
async def scrape_today():
    today = datetime.date.today().strftime('%Y/%m/%d')
    results = await scraper.scrape_multiple_keywords(
        keywords=["環境監測", "土壤", "地下水", "環境"],
        start_date=today,
        end_date=today,
        page_size=100
    )
    n8n_format_results = [{"json": item} for item in results]
    return n8n_format_results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
