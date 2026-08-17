import urllib.parse
url = "https://rera.karnataka.gov.in/download_jc?DOC_ID=JDIkzJvToWYZRmgJD4fmDQ%3D%3D"
print("Original:", url)
print("Double unquote:", urllib.parse.unquote(url))
