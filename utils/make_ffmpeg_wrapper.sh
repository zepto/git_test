#!/bin/bash
h2xml.py -c -I/usr/include/libavcodec -I/usr/include/libavformat -I/usr/include/libavdevice -I/usr/include/libswscale -I/usr/include/libavfilter -I/usr/include/libpostproc /usr/include/libavcodec/avcodec.h /usr/include/libavformat/avformat.h /usr/include/libswscale/swscale.h /usr/include/libavdevice/avdevice.h /usr/include/libavfilter/avfilter.h /usr/include/libpostproc/postprocess.h -o av.xml -D __STDC_CONSTANT_MACROS
xml2py av.xml -o av.py -l /usr/lib/libavcodec.so -l /usr/lib/libavformat.so -l /usr/lib/libswscale.so -l /usr/lib/libavdevice.so -l /usr/lib/libavfilter.so -l /usr/lib/libpostproc.so
sed -i 's/\([[:digit:]]\)L\>/\1/g' av.py
