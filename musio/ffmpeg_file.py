#!/usr/bin/env python
# vim: sw=4:ts=4:sts=4:fdm=indent:fdl=0:
# -*- coding: UTF8 -*-
#
# A module to handle the reading of media files using ffmpeg.
# Copyright (C) 2012 Josiah Gordon <josiahg@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


"""A module for reading media files using ffmpeg."""

import sys
from array import array
from typing import Any

from .import_util import LazyImport
from .io_base import AudioIO, io_wrapper
from .io_util import msg_out, silence
from .portaudio_io import Portaudio

_av = LazyImport('ffmpeg.av', globals(), locals(), ['av'], 1)

__supported_dict = {
    'ext': ['.webm', '.flv', '.iflv', '.wma', '.wmv', '.avi', '.mpg', '.m4a',
            '.oga', '.aac', '.flac', '.mp4',
            '.m4v', '.mp2'],
    'protocol': ['http'],
    'handler': 'FFmpegFile',
    'dependencies': {
        'ctypes': ['avcodec', 'avdevice', 'avformat', 'postproc', 'swscale'],
        'python': []
    }
}


class FFmpegFile(AudioIO):
    """A file like object for reading media files with ffmpeg."""

    # Only reading is supported
    _supported_modes = 'rw'

    def __init__(self, filename: str, mode: str = 'r', depth: int = 16,
                 rate: int = 44100, channels: int = 2, floatp: bool = False,
                 unsigned: bool = False, resample: bool = True,
                 bit_rate: int = 12800, comment_dict: dict = {}, **_):
        """Initialize the playback settings of the player.

        filename -> The file to play.
        depth    -> The bit depth to output when read is called.
        rate     -> The sample rate to output on read.
        channels -> The number of channels in output data.
        floatp   -> Output floating point data.
        unsigned -> Output unsigned data.
        resample -> Resample audio to the specified values if True
                    else use the files values.
        """
        super(FFmpegFile, self).__init__(filename, mode=mode, depth=depth,
                                         rate=rate, channels=channels)

        # Initialize ffmpeg.
        _av.avdevice_register_all()

        self.__network_stream = False
        self._resample_data = resample
        self._unsigned = unsigned
        self._floatp = floatp
        self._depth = 32 if self._floatp else depth
        self._bit_rate = bit_rate
        self._channel_layout = _av.av_get_default_channel_layout(
            self._channels
        )

        self._position = 0
        self._seek_pos = -1

        self._data = b''

        # Open the file.
        if mode == 'r':
            (
                self.__codec_context,
                self.__audio_stream,
                self.__format_context
            ) = self._read_open(filename)

            self._bit_rate = self.__codec_context.contents.bit_rate

            # Update the file info.
            if not resample:
                self._rate = int(self.__codec_context.contents.sample_rate)
                self._channels = int(self.__codec_context.contents.channels)

                # Get the bit depth.
                codec_depth = _av.av_get_bytes_per_sample(
                    self.__codec_context.contents.sample_fmt
                )

                self._depth = codec_depth * 8

                # Use the sample format string to determine the depth and
                # whether it is signed.
                d_str = _av.av_get_sample_fmt_name(
                    self.__codec_context.contents.sample_fmt
                )
                d_str = d_str.decode()

                # Extract the signed and depth properties from the sample
                # format string.
                self._unsigned = 'u' in d_str.lower()
                # self._floatp = (
                #     d_str.lower() in ['fltp', 'flt', 'dblp', 'dbl']
                # )
                # self._sample_fmt = self.__codec_context.contents.sample_fmt

            self._sample_fmt = self._get_sample_fmt()

            # Create the resampling context.
            self._swr = self._get_swr(self.__codec_context)

            # The length of the file is in av time base units.  To get the
            # actual time devide it by 1000, but we use this to seek.
            self._length = self.__format_context.contents.duration
        else:
            self._comment_dict = comment_dict
            (
                self.__format_context,
                self.__codec_context
            ) = self._write_open(filename)

            self._unsigned = True if depth < 16 else self._unsigned

            self._sample_fmt = self._get_sample_fmt()

            # Create the resampling context.
            self._swr = self._get_swr(self.__codec_context)

            # Create the frame to hold the output data for encoding.
            self.__frame = self._alloc_frame(
                self.__codec_context.contents.sample_rate,
                self.__codec_context.contents.sample_fmt,
                self.__codec_context.contents.channel_layout,
                self.__codec_context.contents.channels
            )

            # dev_depth = _av.av_get_bytes_per_sample(
            #     self.__frame.contents.format) * 8
            # self._dev = Portaudio(depth=dev_depth,
            #                       rate=self.__frame.contents.sample_rate,
            #                       channels=self.__frame.contents.channels,
            #                       floatp=(self.__frame.contents.format in
            #                               (3, 4, 8, 9)))
            # # self._dev = open_device(self)

            sample_size = _av.av_get_bytes_per_sample(self._sample_fmt)

            # The size of the input buffer is the number of samples per channel
            # times the number of channels times the sample size.
            self._buffer_len = (
                self.__frame.contents.nb_samples * self._channels * sample_size
            )

            # Create the frame to hold the input data for resampling.
            self.__input_frame = self._alloc_frame(
                self._rate,
                self._sample_fmt,
                self._channel_layout,
                self._channels
            )

            ret = self._check(_av.av_frame_make_writable(self.__input_frame))
            if ret < 0:
                print('Unable to make input frame writable.')
                sys.exit(1)

            self._next_pts = 0
            self._samples_count = 0

    def __repr__(self) -> str:
        """Return a python expression to recreate this instance."""
        if self._mode == 'r':
            repr_str = (f"filename='{self._filename}', mode={self._mode}, "
                        f"depth={self._depth}, rate={self._rate}, "
                        f"channels={self._channels}, floatp={self._floatp}, "
                        f"unsigned={self._unsigned}")
        else:
            repr_str = (f"filename='{self._filename}', mode={self._mode}, "
                        f"depth={self._depth}, rate={self._rate}, "
                        f"channels={self._channels}, "
                        f"floatp={self._floatp}, unsigned={self._unsigned}, "
                        f"comment_dict={self._comment_dict}")

        return f"{self.__class__.__name__}({repr_str})"

    def _check(self, ret: int) -> int:
        """Check if there was an error and print the result."""
        if ret < 0:
            errbuf = _av.create_string_buffer(128)
            _av.av_strerror(ret, errbuf, _av.sizeof(errbuf))
            msg_out(f"{ret}: {errbuf.raw.decode('utf8', 'replace')}")

        return ret

    def _get_sample_fmt(self) -> int:
        """Return the sample format of the output audio."""
        signed_char = ('' if self._floatp else
                       'U' if self._unsigned else 'S')
        return getattr(
            _av,
            f"AV_SAMPLE_FMT_{signed_char}"
            f"{'FLT' if self._floatp else self._depth}"
        )

    def _set_position(self, position: int):
        """Change the position of playback."""
        # We have to seek when the stream is ready not now.
        self._seek_pos = position

    def _get_position(self) -> int:
        """Return the current position."""
        return self._position
        # return _av.avio_seek(
        #     self.__format_context.contents.pb,
        #     0,
        #     1  # SEEK_CUR
        # )

        # Update the position.
        # stream = self.__format_context.contents.streams[self.__audio_stream]
        # We have to multiply the current position by the time base units so it
        # will correspond to the duration, and allow us to seek.
        # return stream.contents.cur_dts * stream.contents.time_base.den
        # return stream.contents.cur_dts * stream.contents.time_base.den

    def _set_metadata(self, metadata: Any):
        """Set the metadata from the comments dict."""
        for key, value in self._comment_dict.items():
            _ = self._check(_av.av_dict_set(
                metadata,
                key.encode(),
                value.encode(),
                0
            ))

    def _get_metadata(self, metadata: Any) -> dict:
        """Get the metadata from the metadata AVDictionary."""
        return_dict = {}

        # Get the first item.
        prev_item = _av.av_dict_get(
            metadata,
            "",
            None,
            _av.AV_DICT_IGNORE_SUFFIX
        )
        # Loop over all the items.
        while prev_item:
            key = str(prev_item.contents.key).replace('_', ' ')
            value = str(prev_item.contents.value)
            return_dict[key] = value
            # Get the next item.
            prev_item = _av.av_dict_get(
                metadata,
                "",
                prev_item,
                _av.AV_DICT_IGNORE_SUFFIX
            )

        return return_dict

    def _write_open(self, filename: str) -> tuple[Any, Any]:
        """Open the specified file."""
        # Create a bytes version of the filename to use with ctypes functions.
        filename_b = filename.encode('utf-8', 'surrogateescape')

        format_context = _av.POINTER(_av.AVFormatContext)()
        ret = self._check(_av.avformat_alloc_output_context2(
            _av.byref(format_context),
            None,
            None,
            filename_b
        ))

        if ret < 0:
            print("Could not deduce output format from filename: using opus.")
            ret = self._check(_av.avformat_alloc_output_context2(
                _av.byref(format_context),
                None,
                b'opus',
                filename_b
            ))
        if ret < 0:
            sys.exit(1)

        codec_id = format_context.contents.oformat.contents.audio_codec
        if codec_id == _av.AV_CODEC_ID_NONE:
            print("No output codec found.")
            sys.exit(1)

        codec = _av.avcodec_find_encoder(codec_id)
        if not codec:
            print(f"No encoder found for {_av.avcodec_get_name(codec_id)}")
            sys.exit(1)

        output_stream = _av.avformat_new_stream(format_context, None)
        if not output_stream:
            print("Could not create stream.")
            sys.exit(1)

        codec_context = _av.avcodec_alloc_context3(codec)
        if not codec_context:
            print("Could not create codec context.")
            sys.exit(1)

        codec_sample_fmts = codec.contents.sample_fmts
        if codec_sample_fmts:
            codec_context.contents.sample_fmt = codec_sample_fmts[0]
        else:
            codec_context.contents.sample_fmt = _av.AV_SAMPLE_FMT_FLTP

        codec_context.contents.bit_rate = self._bit_rate

        codec_context.contents.sample_rate = self._rate
        if codec.contents.supported_samplerates:
            codec_context.contents.sample_rate = (
                codec.contents.supported_samplerates[0]
            )
            i = 0
            while codec.contents.supported_samplerates[i]:
                if codec.contents.supported_samplerates[i] == self._rate:
                    codec_context.contents.sample_rate = self._rate
                i += 1

        codec_context.contents.channels = self._channels

        codec_context.contents.channel_layout = self._channel_layout
        codec_channel_layouts = codec.contents.channel_layouts
        if codec_channel_layouts:
            codec_context.contents.channel_layout = codec_channel_layouts[0]
            i = 0
            while codec_channel_layouts[i]:
                if codec_channel_layouts[i] == self._channel_layout:
                    codec_context.contents.channel_layout = (
                        self._channel_layout
                    )
                i += 1
        codec_context.contents.channels = (
            _av.av_get_channel_layout_nb_channels(
                codec_context.contents.channel_layout
            )
        )

        time_base = _av.AVRational()
        time_base.num = 1
        time_base.den = codec_context.contents.sample_rate
        codec_context.contents.time_base = time_base

        # Open the codec context.
        self._check(_av.avcodec_open2(codec_context, codec, None))

        # Set the stream parameters from the codec_context.
        self._check(_av.avcodec_parameters_from_context(
            output_stream.contents.codecpar, codec_context
        ))

        # Show the format information.
        with silence(sys.stderr):
            _av.av_dump_format(
                format_context,
                0,
                filename_b,
                1
            )

        ret = self._check(_av.avio_open(
            _av.byref(format_context.contents.pb),
            filename_b,
            _av.AVIO_FLAG_WRITE
        ))
        if ret < 0:
            print(f"Error opening {filename}")
            sys.exit(1)

        # Set the metadata.
        self._set_metadata(format_context.contents.metadata)

        # Write the header.
        ret = self._check(_av.avformat_write_header(format_context, None))
        if ret < 0:
            print(f"Error writing the header in {filename}")
            sys.exit(1)

        self._closed = False

        return format_context, codec_context

    def _alloc_frame(self, sample_rate: int, sample_fmt: int,
                     channel_layout: int, channels: int) -> Any:
        """Allocate a frame and set its parameters."""
        if (self.__codec_context.contents.codec.contents.capabilities
                & _av.AV_CODEC_CAP_VARIABLE_FRAME_SIZE):
            nb_samples = 10000
        else:
            nb_samples = self.__codec_context.contents.frame_size

        frame = _av.av_frame_alloc()

        frame.contents.sample_rate = sample_rate
        frame.contents.nb_samples = nb_samples
        frame.contents.format = sample_fmt
        frame.contents.channel_layout = channel_layout
        frame.contents.channels = channels

        if nb_samples:
            ret = _av.av_frame_get_buffer(frame, 0)
            if ret < 0:
                print("Unable to allocate frame buffer.")
                sys.exit()

        return frame

    def write(self, data: bytes) -> int:
        """Write data to file and return how much was written."""
        self._data += data
        if len(self._data) < self._buffer_len:
            return 0
        out_data = self._data[:self._buffer_len]
        self._data = self._data[self._buffer_len:]

        return self._encode(out_data)

    def _encode(self, data: bytes) -> int:
        """Encode the data and return the length."""
        self.__frame.contents.pts = self._next_pts
        self._next_pts += self.__frame.contents.nb_samples

        out_linesize = self._resample_from_bytes(data)

        if out_linesize < 0:
            print("Error resampling.")
            sys.exit(1)
        elif not out_linesize and not data:
            return 0

        # test = self._get_interleaved_data(self.__frame)
        # self._dev.write(test)

        ret = self._check(_av.avcodec_send_frame(
            self.__codec_context,
            self.__frame
        ))
        if ret < 0:
            print('problem with send frame')
            return 0
        packet = _av.av_packet_alloc()
        while ret >= 0:
            ret = self._check(_av.avcodec_receive_packet(
                self.__codec_context,
                packet
            ))
            if ret in (-11, -541478725):  # EAGAIN, AVERROR_EOF
                break
            elif ret < 0:
                print("Error")
                break

            _av.av_packet_rescale_ts(
                packet,
                self.__codec_context.contents.time_base,
                self.__format_context.contents.streams[0].contents.time_base
            )
            packet.contents.stream_index = 0

            ret = self._check(_av.av_interleaved_write_frame(
                self.__format_context,
                packet if data else None
            ))
            _av.av_packet_unref(packet)

        return len(data)

    def _read_open(self, filename: str) -> tuple[Any, int, Any]:
        """Load the specified file."""
        # Create a bytes version of the filename to use with ctypes functions.
        filename_b = filename.encode('utf-8', 'surrogateescape')

        # Check if it is a network stream.
        if b'://' in filename_b:
            _av.avformat_network_init()
            self.__network_stream = True

        # Create a format context, and open the file.
        format_context = _av.avformat_alloc_context()
        self._check(_av.avformat_open_input(
            format_context,
            filename_b,
            None,
            None
        ))
        # Find the stream info.
        self._check(_av.avformat_find_stream_info(format_context, None))

        # Temorary variables used for finding the audio stream.
        # nb_streams = format_context.contents.nb_streams
        # streams = format_context.contents.streams

        # audio_stream_index = 0
        # stream = _av.AVStream()
        # for i in range(nb_streams):
        #     codec_type = streams[i].contents.codec.contents.codec_type
        #     if codec_type == _av.AVMEDIA_TYPE_AUDIO:
        #         # Remember audio stream index.
        #         audio_stream_index = i
        #         # Save the audio stream and exit the loop.
        #         stream = streams[i]
        #         break

        # Allocate space for the decoder codec.
        codec = _av.POINTER(_av.AVCodec)()

        # Determine which stream is the audio stream and put the decoder for it
        # in codec.
        audio_stream_index = self._check(_av.av_find_best_stream(
            format_context,
            _av.AVMEDIA_TYPE_AUDIO,
            -1,
            -1,
            _av.byref(codec),
            0
        ))
        if audio_stream_index < 0:
            msg_out("No audio stream was found.")
            sys.exit()

        with silence(sys.stderr):
            _av.av_dump_format(
                format_context,
                audio_stream_index,
                filename_b,
                0
            )

        # Get the stream.
        stream = format_context.contents.streams[audio_stream_index]

        # Grab the files metadata.
        self._info_dict.update(self._get_metadata(
            format_context.contents.metadata
        ))
        # Grab the stream metadata.
        self._info_dict.update(self._get_metadata(
            stream.contents.metadata
        ))

        # Allocate space for the codec context.
        codec_context = _av.avcodec_alloc_context3(None)

        # Create a codec context from the stream parameters.
        self._check(_av.avcodec_parameters_to_context(
            codec_context,
            stream.contents.codecpar
        ))
        codec_context.contents.pkt_timebase = stream.contents.time_base

        # Find the codec to decode the audio.
        # codec = _av.avcodec_find_decoder(
        #     stream.contents.codec.contents.codec_id
        # )

        # Get the codec and open a codec context from it.
        self._check(_av.avcodec_open2(
            codec_context,
            codec,
            None
        ))

        # The file is now open.
        self._closed = False

        return codec_context, audio_stream_index, format_context

    def _get_swr(self, codec_context: Any) -> Any:
        """Return an allocated SWResamleContext."""
        swr = _av.swr_alloc()
        if not swr:
            raise(Exception("Unable to allocate avresample context"))

        if self._mode == 'r':
            if codec_context.contents.channel_layout == 0:
                # If the channel layout is invalid set it based on the number
                # of channels defined in the codec_context.
                in_channel_layout = _av.av_get_default_channel_layout(
                    codec_context.contents.channels
                )
            else:
                in_channel_layout = codec_context.contents.channel_layout
            in_sample_fmt = codec_context.contents.sample_fmt
            in_sample_rate = codec_context.contents.sample_rate

            out_channel_layout = self._channel_layout
            out_sample_fmt = self._sample_fmt
            out_sample_rate = self._rate
        else:
            in_channel_layout = self._channel_layout
            in_sample_fmt = self._sample_fmt
            in_sample_rate = self._rate

            out_channel_layout = codec_context.contents.channel_layout
            out_sample_fmt = codec_context.contents.sample_fmt
            out_sample_rate = codec_context.contents.sample_rate

        _av.swr_alloc_set_opts(
            swr,
            # Output options.
            out_channel_layout,
            out_sample_fmt,
            out_sample_rate,
            # Input options
            in_channel_layout,
            in_sample_fmt,
            in_sample_rate,
            # Logging options
            0,
            None
        )

        self._check(_av.swr_init(swr))

        return swr

    def _drain_decoder(self, av_packet: Any, frame: Any) -> bytes:
        """Drain the decoder and return the resulting bytes."""
        # Send packet to the decoder to decode.
        ret = self._check(_av.avcodec_send_packet(
            self.__codec_context, av_packet
        ))
        if ret == 0:
            # Recieve the decoded data, from the decoder, into frame.
            ret = self._check(_av.avcodec_receive_frame(
                self.__codec_context,
                frame
            ))
            # Return nothing on and error other than EOF and EAGAIN.
            if ret < 0 and ret not in (-11, -541478725):
                return b''
        else:
            return b''

        # Resample or return interleaved data.
        if self._resample_data or (self._sample_fmt != frame.contents.format):
            return self._resample_from_frame(frame)
        else:
            return self._get_interleaved_data(frame)

    @io_wrapper
    def read(self, size: int) -> bytes:
        """Read size amount of data and return it.

        If size is -1 read the entire file.
        """
        data = self._data

        # Create the packet to decode from.
        av_packet = _av.av_packet_alloc()

        # Create and setup a frame to read the data into.
        frame = _av.av_frame_alloc()

        # Seek before next read begins.
        if self._seek_pos > -1:
            with silence(sys.stderr):
                ret = _av.avformat_seek_file(
                    self.__format_context,
                    -1,
                    -sys.maxsize - 1,
                    self._seek_pos,
                    sys.maxsize,
                    1
                )
            self._check(ret)

            _av.avcodec_flush_buffers(self.__codec_context)

            # Reset the seek so we don't continue seeking.
            self._seek_pos = -1

        while not data or len(data) < size:
            # Read the next frame.
            if _av.av_read_frame(self.__format_context, av_packet) < 0:
                data += self._drain_decoder(av_packet, frame)
                # If no data was read then we have reached the end of the
                # file so restart or exit.
                if self._loops != -1 and self._loop_count >= self._loops:
                    # Fill the data buffer with nothing so it will be a
                    # frame size for output.
                    if len(data) != 0:
                        data += b'\x00' * (size - len(data))
                else:
                    # Fill the buffer so we return the requested size.
                    data += b'\x00' * (size - len(data))

                    # Update the loop count and seek to the start.
                    self._loop_count += 1
                    self.seek(0)

                # Exit.
                break

            # Calculate the current position.
            stream = self.__format_context.contents.streams[
                self.__audio_stream
            ]
            self._position = (1000000 *
                              (av_packet.contents.pts *
                               (stream.contents.time_base.num /
                                stream.contents.time_base.den)))

            # If the packet read is not audio then skip it.
            if av_packet.contents.stream_index != self.__audio_stream:
                _av.av_packet_unref(av_packet)
                continue

            # Reset the frame, (I don't know if this is necessary).
            _av.av_frame_unref(frame)

            # data_size = _av.av_samples_get_buffer_size(
            #     None,
            #     frame.contents.channels,
            #     frame.contents.nb_samples,
            #     frame.contents.format,
            #     1
            # )

            # Get the decoded data.
            data += self._drain_decoder(av_packet, frame)

            # Unreference the packet resetting it to default.
            _av.av_packet_unref(av_packet)

        # Free the frame and packet.
        _av.av_frame_unref(frame)
        _av.av_frame_free(_av.byref(frame))
        _av.av_packet_unref(av_packet)
        _av.av_packet_free(_av.byref(av_packet))

        # Store extra data for next time.
        self._data = data[size:]

        # Make sure we return only the number of bytes requested.
        return data[:size]

    def _get_interleaved_data(self, frame: Any) -> bytes:
        """Process the data in frame and return it.

        If the data in frame is planar data, interleave it and return the
        result.  If it is already interleaved, than just return it as a bytes
        object.
        """
        if _av.av_sample_fmt_is_planar(frame.contents.format):
            # Get the sample_size to determint the data type for the output.
            sample_size = _av.av_get_bytes_per_sample(frame.contents.format)
            # Array to hold the output data.
            data_array = array('l' if sample_size == 8 else
                               'i' if sample_size == 4 else
                               'h' if sample_size == 2 else
                               'b')
            # The data type to cast the data to.
            cast_to_type = (_av.c_int64 if sample_size == 8 else
                            _av.c_int32 if sample_size == 4 else
                            _av.c_int16 if sample_size == 2 else
                            _av.c_uint8)

            # Interleave data into data_array
            nb_samples = frame.contents.nb_samples
            # Loop over the number of samples.
            for s in range(nb_samples):
                # Loop over the channels.
                for c in range(frame.contents.channels):
                    # Cast the data to the correct size data type.
                    temp_data = _av.cast(
                        frame.contents.extended_data[c],
                        _av.POINTER(cast_to_type)
                    )[s]
                    # Append the data to the array
                    data_array.append(temp_data)
            # Return the data_array as bytes.
            return data_array.tobytes()
        else:
            # The input was already interleaved so just send it back.
            return _av.string_at(
                frame.contents.extended_data[0],
                frame.contents.linesize[0]
            )

    def _resample_from_bytes(self, data: bytes) -> int:
        """Resample the data into self.__frame and return out_linesize."""
        # Don't resample null data.
        ret = self._check(_av.av_frame_make_writable(self.__frame))
        if ret < 0:
            print('av_frame_make_writable')
            sys.exit(1)

        self.__input_frame.contents.extended_data[0] = _av.cast(
            _av.create_string_buffer(data),
            _av.POINTER(_av.c_uint8)
        )

        return _av.swr_convert_frame(
            self._swr,
            self.__frame,
            self.__input_frame if data else None
        )

        # out_samples = _av.av_rescale_rnd(
        #     # Set the delay to output samples.
        #     _av.swr_get_delay(
        #         self._swr,
        #         self._rate
        #     ) + self.__frame.contents.nb_samples,      # Samples per channel.
        #     self.__codec_context.contents.sample_rate,  # Output sample rate.
        #     self._rate,                                 # Input sample rate.
        #     _av.AV_ROUND_UP
        # )
        # assert(self.__frame.contents.nb_samples == out_samples)
        #
        # # Resample the data.
        # return _av.swr_convert(
        #     self._swr,
        #     # Output.
        #     self.__frame.contents.extended_data,
        #     out_samples,
        #     # Input.
        #     _av.cast(
        #         _av.create_string_buffer(data),
        #         _av.POINTER(_av.c_uint8)
        #     ) if data else None,
        #     self.__frame.contents.nb_samples if data else 0
        # )

    def _resample_from_frame(self, frame: Any) -> bytes:
        """Resample the data in frame and return it."""
        # Don't resample null data.
        if not frame.contents.linesize[0]:
            return b''

        output = _av.POINTER(_av.c_uint8)()
        out_linesize = _av.c_int()

        # Calculate how many resampled samples there will be.
        out_samples = _av.av_rescale_rnd(
            # Set the delay to output samples.
            _av.swr_get_delay(
                self._swr,
                self.__codec_context.contents.sample_rate
            ) + frame.contents.nb_samples,              # Samples per channel.
            self._rate,                                 # Output sample rate.
            self.__codec_context.contents.sample_rate,  # Input sample rate.
            _av.AV_ROUND_UP
        )

        # Allocate a buffer large enough to hold the resampled data.
        _av.av_samples_alloc(
            _av.byref(output),          # Array for pointers to each channel.
            # _av.byref(out_linesize),    # Aligned size of audio buffers.
            None,
            self._channels,             # Use output channels here.
            out_samples,                # Number of samples per channel.
            self._sample_fmt,           # Output sample format.
            0                           # Buffer size alignment.
        )

        # Resample the data in the frame to match the settings in avr.
        out_linesize = _av.swr_convert(
            self._swr,
            # Output.
            _av.byref(output),             # Destination of the resampled data.
            out_samples,                   # Number of samples per channel.
            # Input.
            frame.contents.extended_data,  # Input data.
            frame.contents.nb_samples      # Number of samples per channel.
        )

        data = b''
        while out_linesize > 0:
            # Get the bytes in the output buffer.
            data += _av.string_at(
                output,
                out_linesize
                * self._channels
                * _av.av_get_bytes_per_sample(self._sample_fmt)
            )

            # Flush the resample buffer.
            out_linesize = _av.swr_convert(
                self._swr,
                # Output.
                _av.byref(output),    # Destination of the resampled data.
                out_samples,          # Number of samples per channel.
                # Input.
                None,                 # Input data.
                0                     # Number of samples per channel.
            )

        # data = _av.string_at(
        #     output,
        #     out_linesize
        #     * self._channels
        #     * _av.av_get_bytes_per_sample(self._sample_fmt)
        # )

        # Free the output buffer.
        _av.av_freep(_av.byref(output))

        return data

    def close(self):
        """Close and cleans up."""
        if not self.closed:
            if self._mode == 'w':
                while self.write(b''):
                    pass
                self._encode(b'')

                _av.av_write_trailer(self.__format_context)

            # Close and free the resample context.
            _av.swr_close(self._swr)
            _av.swr_free(self._swr)

            # Close the file and free all other contexts.
            if self._mode == 'w':
                _av.av_frame_free(_av.byref(self.__frame))
                _av.av_frame_free(self.__input_frame)
                _av.avio_closep(_av.byref(self.__format_context.contents.pb))
            else:
                _av.avformat_close_input(self.__format_context)

            _av.avcodec_free_context(self.__codec_context)

            _av.avformat_free_context(self.__format_context)

            # Delete the data structures.
            del(self.__format_context)
            del(self.__codec_context)

            # Deinit the network if the file read was a network stream.
            if self.__network_stream:
                _av.avformat_network_deinit()

            # This file is closed.
            self._closed = True
