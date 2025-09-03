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
                # 這裡的 keys 順序也需要確認一下是否和實際 cols 順序一致
                # 根據 HTML，cols[2] 是 標案案號&名稱, cols[3] 是 傳輸次數
                # 你定義的 keys 是 ["項次", "機關名稱", "標案案號&編號名稱", "傳輸次數", ...]
                # 這意味著 cols[2] 應該對應 "標案案號&編號名稱"
                
                # 直接提取文本，稍後再細分
                row_data_raw = [col.get_text("\n", strip=True) for col in cols[:9]]
                
                # 重新定義 keys 以確保與實際抓取內容的對應
                # 由於 cols[2] 同時包含案號和名稱，我們將其視為一個原始字段
                # 其他字段按順序對應
                keys = ["項次", "機關名稱", "標案編號_與_名稱_原始", "傳輸次數", "招標方式", "採購性質", "公告日期", "截止投標", "預算金額"]
                row_dict = dict(zip(keys, row_data_raw))

                # 提取連結
                href_url = ""
                # 定位到包含案號和名稱的<td>，即 cols[2]
                target_td = cols[2] 
                a_tag = target_td.find("a")
                if a_tag and a_tag.get('href'): # 不再需要 'pk=' 判斷，直接取 href
                    pk_val = a_tag['href'].split('pk=')[-1].split('&')[0] if 'pk=' in a_tag['href'] else ''
                    href_url = f"https://web.pcc.gov.tw/tps/QueryTender/query/searchTenderDetail?pkPmsMain={pk_val}"
                
                # 分開案號與名稱
                # 案號通常是 <td> 的直接文本節點的一部分，在 <br> 之前
                # 案名是 <a> 標籤內的 <span> 文本
                
                tender_id = ""
                tender_name = ""

                # 案號：在 <br> 標籤之前的所有文本內容
                # 使用 .contents 獲取所有子節點，然後檢查文本節點
                for content in target_td.contents:
                    if content.name == 'br':
                        break # 遇到 <br> 就停止，前面的文字是案號的一部分
                    if isinstance(content, str) and content.strip():
                        tender_id += content.strip()
                    elif content.name == 'span' and content.get_text(strip=True):
                        # 處理 "(更正公告)" 這種在案號後面的 span
                        tender_id += " " + content.get_text(strip=True)
                
                # 清理案號，通常在案號後面會有不必要的文字，例如 "(更正公告)"，所以只取第一個詞或更精確的模式
                # 可以嘗試用正則表達式，但簡單處理先移除括號內容
                tender_id = tender_id.split('(')[0].strip() # 移除像 "(更正公告)" 的部分

                # 案名：從 <a> 標籤內的 <u> 標籤中的 <span> 提取
                tender_name_span = target_td.find("a", recursive=False) # 直接查找子層的 <a>
                if tender_name_span:
                    tender_name = tender_name_span.get_text(strip=True)
                    
                final_data = {
                    "項次": row_dict.get("項次", ""),
                    "機關名稱": row_dict.get("機關名稱", ""),
                    "標案編號": tender_id,
                    "標案名稱": tender_name,
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
