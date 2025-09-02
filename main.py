from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import datetime
import json
import re
import logging
from urllib.parse import urlencode, quote
import time

# 設定日誌記錄
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 建立 FastAPI 應用程式（就像建立一個網站）
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
        self.results = []
        
    async def init_session(self):
        """初始化網路連線"""
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
        """關閉網路連線"""
        if self.session:
            await self.session.close()
            self.session = None
    
    async def scrape_by_keyword(self, keyword: str, start_date: str, end_date: str, page_size: int = 100) -> List[Dict]:
        """根據關鍵字爬取資料"""
        if not self.session:
            await self.init_session()
            
        # 建立查詢參數
        params = {
            'pageSize': page_size,
            'tenderStartDate': start_date,
            'tenderEndDate': end_date,
            'tenderName': keyword,
            'dateType': 'isDate'
        }
        
        # 建立完整網址
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
        """解析網頁內容"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        
        # 尋找資料表格
        table = soup.find('table', {'id': 'tpam'}) or soup.find('table', {'class': 'tb_01'})
        
        if not table:
            logger.warning(f"關鍵字 {keyword} 未找到資料表格")
            return []
        
        rows = table.find_all('tr')
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        for row in rows[1:]:  # 跳過表頭
            cols = row.find_all('td')
            if len(cols) < 9:
                continue
            
            try:
                # 提取基本資料
                row_data = [col.get_text().strip() for col in cols[:9]]
                
                # 建立基本資料字典
                keys = ["項次", "機關名稱", "標案案號&編號名稱", "傳輸次數", "招標方式", "採購性質", "公告日期", "截止投標", "預算金額"]
                row_dict = dict(zip(keys, row_data))
                
                # 提取連結
                href_url = ""
                a_tag = row.find('a')
                if a_tag and a_tag.get('href'):
                    href = a_tag.get('href')
                    if 'pk=' in href:
                        pk_val = href.split('pk=')[-1].split('&')[0]
                        href_url = f"https://web.pcc.gov.tw/tps/QueryTender/query/searchTenderDetail?pkPmsMain={pk_val}"

               # 拆解標案案號&編號名稱
                raw = row_dict.pop("標案案號&編號名稱", "")
                if "\n" in raw:
                    line1, line2 = raw.split("\n", 1)
                    編號 = line1.split()[0] if line1.split() else ""
                    名稱 = line2.strip()
                else:
                    編號 = ""
                    名稱 = raw
                
                # 組織最終結果
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
                
                # 只保留有效資料
                if 名稱 and row_dict.get("機關名稱"):
                    results.append(final_data)
                    
            except Exception as e:
                logger.error(f"解析行資料時發生錯誤: {str(e)}")
                continue
        
        logger.info(f"關鍵字 {keyword} 獲得 {len(results)} 筆資料")
        return results
    
    async def scrape_multiple_keywords(self, keywords: List[str], start_date: str, end_date: str, page_size: int = 100) -> List[Dict]:
        """爬取多個關鍵字"""
        all_results = []
        
        for keyword in keywords:
            try:
                # 每個關鍵字之間等待一段時間，避免過度請求
                if all_results:  # 不是第一次請求時才等待
                    await asyncio.sleep(2)
                
                keyword_results = await self.scrape_by_keyword(keyword, start_date, end_date, page_size)
                all_results.extend(keyword_results)
                
            except Exception as e:
                logger.error(f"爬取關鍵字 {keyword} 時發生錯誤: {str(e)}")
                continue
        
        return all_results

# 建立爬蟲實例
scraper = PCCWebScraper()

@app.on_event("startup")
async def startup_event():
    """網站啟動時初始化"""
    await scraper.init_session()
    logger.info("政府採購網爬蟲 API 服務已啟動")

@app.on_event("shutdown")
async def shutdown_event():
    """網站關閉時清理資源"""
    await scraper.close_session()
    logger.info("政府採購網爬蟲 API 服務已關閉")

@app.get("/")
async def root():
    """首頁 - 顯示 API 資訊"""
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
    """檢查服務是否正常"""
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now().isoformat(),
        "service": "政府採購網爬蟲"
    }

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tenders(request: ScrapeRequest):
    """
    爬取政府採購網資料
    
    - **search_terms**: 搜尋關鍵字列表
    - **start_date**: 開始日期 (YYYY/MM/DD)
    - **end_date**: 結束日期 (YYYY/MM/DD)
    - **page_size**: 每頁資料數量
    """
    try:
        # 設定預設日期
        if not request.start_date:
            request.start_date = datetime.date.today().strftime('%Y/%m/%d')
        if not request.end_date:
            request.end_date = datetime.date.today().strftime('%Y/%m/%d')
        
        logger.info(f"開始爬取 - 關鍵字: {request.search_terms}")
        logger.info(f"日期範圍: {request.start_date} ~ {request.end_date}")
        
        # 執行爬蟲
        results = await scraper.scrape_multiple_keywords(
            keywords=request.search_terms,
            start_date=request.start_date,
            end_date=request.end_date,
            page_size=request.page_size
        )
        
        # 轉換為 n8n 格式
        n8n_format_results = [{"json": item} for item in results]
        
        return ScrapeResponse(
            success=True,
            data=n8n_format_results,
            count=len(results),
            message=f"成功爬取 {len(results)} 筆資料",
            timestamp=datetime.datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"爬蟲執行錯誤: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"爬蟲執行失敗: {str(e)}"
        )

@app.post("/scrape-today")
async def scrape_today():
    """爬取今日資料 - 與原 n8n workflow 相容的接口"""
    try:
        today = datetime.date.today().strftime('%Y/%m/%d')
        
        results = await scraper.scrape_multiple_keywords(
            keywords=["系統", "平台", "建置", "維運"],
            start_date=today,
            end_date=today,
            page_size=100
        )
        
        # 直接返回 n8n 格式的資料，與原 Python 腳本輸出相容
        n8n_format_results = [{"json": item} for item in results]
        return n8n_format_results
        
    except Exception as e:
        logger.error(f"今日資料爬取錯誤: {str(e)}")
        # 返回空陣列，與原腳本的錯誤處理相容
        return []

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )
