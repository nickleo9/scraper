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
    """政府採購網爬蟲類別 - 修正版"""
    
    def __init__(self):
        # 使用正確的全文檢索網址
        self.base_url = "https://web.pcc.gov.tw/prkms/tender/common/bulletion/readBulletion"
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
    
    async def scrape_by_keyword(self, keyword: str, start_date: str = None, end_date: str = None, page_size: int = 100) -> List[Dict]:
        if not self.session:
            await self.init_session()
        
        # 取得當前年度（民國年）
        current_year = datetime.date.today().year - 1911
        
        # 使用全文檢索的參數
        params = {
            'querySentence': keyword,  # 全文查詢關鍵字
            'tenderStatusType': '招標',  # 標案種類
            'sortCol': 'TENDER_NOTICE_DATE',  # 排序欄位
            'timeRange': str(current_year),  # 查詢範圍（民國年）
            'pageSize': str(page_size)
        }
        
        query_string = urlencode(params)
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
        """解析網頁內容"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        
        # 修正：查找正確的表格ID
        table = soup.find('table', {'id': 'bulletion'})
        if not table:
            logger.warning(f"關鍵字 {keyword} 未找到資料表格")
            return []
        
        rows = table.find('tbody').find_all('tr') if table.find('tbody') else table.find_all('tr')
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        for row in rows:
            # 跳過表頭
            if row.find('th'):
                continue
                
            cols = row.find_all('td')
            if len(cols) < 10:  # 確保有足夠的欄位
                continue
                
            try:
                # 根據實際HTML結構解析資料
                # 欄位順序：項次、種類、機關名稱、標案案號與名稱、招標公告日期、
                # 決標公告、截止投標、公開閱覽、預告公告、功能選項
                
                # 解析標案案號與名稱（在第4個td）
                tender_td = cols[3]
                tender_id = ""
                tender_name = ""
                href_url = ""
                
                # 找到連結
                a_tag = tender_td.find('a')
                if a_tag:
                    # 取得連結
                    if a_tag.get('href'):
                        href = a_tag['href']
                        if 'pk=' in href:
                            pk = href.split('pk=')[1].split('&')[0] if '&' in href else href.split('pk=')[1]
                            href_url = f"https://web.pcc.gov.tw{href}"
                        else:
                            href_url = f"https://web.pcc.gov.tw{href}"
                    
                    # 解析案號和案名
                    # 案號在 <br> 之前
                    # 案名在 <span> 標籤內
                    a_html = str(a_tag)
                    if '<br' in a_html:
                        # 分割文本
                        parts = a_tag.get_text('\n', strip=True).split('\n')
                        if len(parts) >= 2:
                            tender_id = parts[0].strip()
                            tender_name = parts[1].strip()
                    else:
                        # 如果沒有<br>，嘗試其他解析方式
                        span_tag = a_tag.find('span')
                        if span_tag:
                            tender_name = span_tag.get_text(strip=True)
                            # 案號是a標籤內但在span標籤外的文本
                            for content in a_tag.contents:
                                if isinstance(content, str):
                                    text = content.strip()
                                    if text:
                                        tender_id = text
                                        break
                
                # 建立資料字典
                final_data = {
                    "項次": cols[0].get_text(strip=True) if len(cols) > 0 else "",
                    "機關名稱": cols[2].get_text(strip=True) if len(cols) > 2 else "",
                    "標案編號": tender_id,
                    "標案名稱": tender_name,
                    "招標方式": cols[1].get_text(strip=True) if len(cols) > 1 else "",  # 種類欄位
                    "公告日期": cols[4].get_text(strip=True) if len(cols) > 4 else "",
                    "截止投標": cols[6].get_text(strip=True) if len(cols) > 6 else "",
                    "決標公告": cols[5].get_text(strip=True) if len(cols) > 5 else "",
                    "網址": href_url,
                    "爬取日期": today,
                    "關鍵字": keyword
                }
                
                # 確保有案名和機關名稱才加入結果
                if tender_name and final_data["機關名稱"]:
                    results.append(final_data)
                    
            except Exception as e:
                logger.error(f"解析行資料時發生錯誤: {str(e)}")
                continue
        
        logger.info(f"關鍵字 {keyword} 獲得 {len(results)} 筆資料")
        return results
    
    async def scrape_multiple_keywords(self, keywords: List[str], start_date: str = None, end_date: str = None, page_size: int = 100) -> List[Dict]:
        all_results = []
        for keyword in keywords:
            if all_results:
                await asyncio.sleep(2)  # 避免請求過快
            keyword_results = await self.scrape_by_keyword(keyword, start_date, end_date, page_size)
            all_results.extend(keyword_results)
        
        # 去除重複（根據標案編號和機關名稱）
        unique_results = []
        seen = set()
        for item in all_results:
            key = (item['標案編號'], item['機關名稱'])
            if key not in seen:
                seen.add(key)
                unique_results.append(item)
        
        return unique_results

# 建立爬蟲實例
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
        "threads": "ZN Studio (@nickai216)｜https://www.threads.com/@nickai216",
        "line_community": "https://reurl.cc/1OZNAY",
        "line": "https://lin.ee/Faz0doj",
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
    """爬取指定關鍵字的標案資料"""
    try:
        results = await scraper.scrape_multiple_keywords(
            keywords=request.search_terms,
            start_date=request.start_date,
            end_date=request.end_date,
            page_size=request.page_size
        )
        
        # 格式化為 n8n 格式
        n8n_format_results = [{"json": item} for item in results]
        
        return ScrapeResponse(
            success=True,
            data=n8n_format_results,
            count=len(results),
            message=f"成功爬取 {len(results)} 筆資料",
            timestamp=datetime.datetime.now().isoformat()
        )
    except Exception as e:
        logger.error(f"爬取失敗: {str(e)}")
        return ScrapeResponse(
            success=False,
            data=[],
            count=0,
            message=f"爬取失敗: {str(e)}",
            timestamp=datetime.datetime.now().isoformat()
        )

@app.post("/scrape-today")
async def scrape_today():
    """爬取今日標案（預設關鍵字）"""
    try:
        # 使用預設的關鍵字組合
        keywords = ["環境監測", "土壤", "地下水", "環境"]
        
        results = await scraper.scrape_multiple_keywords(
            keywords=keywords,
            page_size=100
        )
        
        # 直接返回 n8n 格式的陣列
        n8n_format_results = [{"json": item} for item in results]
        return n8n_format_results
        
    except Exception as e:
        logger.error(f"爬取今日標案失敗: {str(e)}")
        return []

@app.get("/test-scrape/{keyword}")
async def test_scrape(keyword: str):
    """測試爬取單一關鍵字"""
    try:
        results = await scraper.scrape_by_keyword(keyword, page_size=10)
        return {
            "success": True,
            "keyword": keyword,
            "count": len(results),
            "data": results
        }
    except Exception as e:
        return {
            "success": False,
            "keyword": keyword,
            "error": str(e)
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
