Tsukikage 运行附件目录
======================

这个目录必须和 Tsukikage.exe 放在同一个文件夹里。

必需文件（7 个 SPICE 内核）
---------------------------
  naif0012.tls
  de440s.bsp
  pck00011.tpc
  gm_de440.tpc
  moon_pa_de440_200625.bpc
  moon_de440_220930.tf
  earth_latest_high_prec.bpc

缺任何一个，启动时会列出缺少的文件并停下；补齐后按 Enter 会自动重新检查。

月面 DEM（用户提供）
--------------------
至少放一个全球 2:1 高程 .tif：
  ldem_*.tif
  SLDEM2015_512.tif
  Lunar_LRO_LOLA_*.tif

色彩可选：
  lroc_color*.tif
  lroc_color*.jpg
  lroc_color.exr

下载 tif / exr：
  https://svs.gsfc.nasa.gov/4720

只放高程 .tif 也能运行（渲染为灰度）。手动复制进来的 .tif 会在启动时自动扫描；
主程序运行期间目录发生变化，也会自动重新加载。通过 App 内置下载器下载的
LOLA 118 m 全球 DEM 同样保存在这个目录，文件是：
  Lunar_LRO_LOLA_Global_LDEM_118m_Mar2014.tif

长周期模式（可选，默认不用）
----------------------------
如果使用 --kernel-mode long，还需要：
  de441_part-1.bsp
  de441_part-2.bsp

不要删除或改名上面的必需文件；DEM 文件可以随时增删。
