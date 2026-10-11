/*
 * pycam-engine : the camera engine of pycam (see docs/architecture.md).
 *
 * Drives libcamera through librpicam_app, and is driven by the pycam application
 * over a Unix socket (one JSON object per line).
 */

#include <csignal>
#include <cstdio>
#include <iostream>
#include <string>
#include <thread>

#include "engine.hpp"

static void usage()
{
	std::cerr << "usage: pycam-engine [--socket PATH] [--zsl] [--viewfinder WxH] [--lores WxH] [--queue N]\n"
				 "                    [--zsl-buffers N] [--zsl-denoise D] [--still-denoise D] [--no-display] [-v]\n"
				 "  --socket PATH     Unix socket to listen on (default /tmp/pycam-engine.sock)\n"
				 "  --zsl             zero shutter lag: sensor at full resolution all the time\n"
				 "  --viewfinder WxH  viewfinder stream size (default 1024x768)\n"
				 "  --lores WxH       analysis stream size (default 320x240)\n"
				 "  --queue N         max captures waiting to be written (default 3)\n"
				 "  --zsl-buffers N   full resolution buffers in ZSL, ~37 MB of CMA each (default 3)\n"
				 "  --zsl-denoise D   colour denoise in ZSL: cdn_off, cdn_fast (default), cdn_hq\n"
				 "  --still-denoise D colour denoise of switch captures (default cdn_hq)\n"
				 "  --no-display      do not use the screen, even if there is one\n"
				 "  -v               more logs (repeat for libcamera logs)\n";
}

static bool parse_size(std::string const &s, unsigned &w, unsigned &h)
{
	return std::sscanf(s.c_str(), "%ux%u", &w, &h) == 2 && w && h;
}

int main(int argc, char *argv[])
{
	pycam::EngineConfig cfg;
	for (int i = 1; i < argc; i++)
	{
		std::string arg = argv[i];
		auto value = [&]() -> std::string
		{
			if (i + 1 >= argc)
				throw std::invalid_argument("missing value for " + arg);
			return argv[++i];
		};
		try
		{
			if (arg == "--socket")
				cfg.socket_path = value();
			else if (arg == "--zsl")
				cfg.zsl = true;
			else if (arg == "--viewfinder")
			{
				if (!parse_size(value(), cfg.vf_width, cfg.vf_height))
					throw std::invalid_argument("bad size for --viewfinder");
			}
			else if (arg == "--lores")
			{
				if (!parse_size(value(), cfg.lores_width, cfg.lores_height))
					throw std::invalid_argument("bad size for --lores");
			}
			else if (arg == "--queue")
				cfg.max_queue = std::stoul(value());
			else if (arg == "--zsl-buffers")
				cfg.zsl_buffers = std::stoul(value());
			else if (arg == "--zsl-denoise")
				cfg.zsl_denoise = value();
			else if (arg == "--still-denoise")
				cfg.still_denoise = value();
			else if (arg == "--no-display")
				cfg.display = false;
			else if (arg == "-v")
				cfg.verbose++;
			else if (arg == "-h" || arg == "--help")
			{
				usage();
				return 0;
			}
			else
				throw std::invalid_argument("unknown argument " + arg);
		}
		catch (std::exception const &e)
		{
			std::cerr << "pycam-engine: " << e.what() << "\n";
			usage();
			return 2;
		}
	}

	// SIGINT/SIGTERM are handled by a dedicated thread so that the camera is always
	// stopped cleanly. Block them before any thread (ours or libcamera's) is created.
	sigset_t quit_signals;
	sigemptyset(&quit_signals);
	sigaddset(&quit_signals, SIGINT);
	sigaddset(&quit_signals, SIGTERM);
	pthread_sigmask(SIG_BLOCK, &quit_signals, nullptr);
	std::signal(SIGPIPE, SIG_IGN);

	try
	{
		pycam::Engine engine(cfg);
		std::thread signal_thread(
			[&]
			{
				int sig;
				sigwait(&quit_signals, &sig);
				engine.RequestQuit();
			});
		signal_thread.detach();
		return engine.Run();
	}
	catch (std::exception const &e)
	{
		std::cerr << "pycam-engine: fatal: " << e.what() << "\n";
		return 1;
	}
}
