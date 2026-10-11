#include "ipc_server.hpp"

#include <cerrno>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>

#include <poll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

using nlohmann::json;

namespace pycam
{

namespace
{

constexpr size_t kMaxInput = 1 << 20; // a client sending a 1 MB line is broken
constexpr size_t kMaxOutput = 4 << 20; // a client not reading 4 MB of events is stuck

std::string errno_string(std::string const &what)
{
	return what + ": " + std::strerror(errno);
}

std::string to_line(json const &message)
{
	return message.dump(-1, ' ', false, json::error_handler_t::replace) + "\n";
}

} // namespace

IpcServer::~IpcServer()
{
	Stop();
}

void IpcServer::Start(std::string const &path, Handler handler)
{
	path_ = path;
	handler_ = std::move(handler);

	sockaddr_un addr {};
	addr.sun_family = AF_UNIX;
	if (path.size() >= sizeof(addr.sun_path))
		throw std::runtime_error("socket path too long: " + path);
	std::memcpy(addr.sun_path, path.c_str(), path.size() + 1);

	std::filesystem::path parent = std::filesystem::path(path).parent_path();
	if (!parent.empty())
		std::filesystem::create_directories(parent);
	::unlink(path.c_str()); // stale socket from a previous run

	listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (listen_fd_ < 0)
		throw std::runtime_error(errno_string("socket"));
	if (::bind(listen_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0)
		throw std::runtime_error(errno_string("bind " + path));
	if (::listen(listen_fd_, 4) < 0)
		throw std::runtime_error(errno_string("listen"));

	wake_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
	if (wake_fd_ < 0)
		throw std::runtime_error(errno_string("eventfd"));

	thread_ = std::thread(&IpcServer::Loop, this);
}

void IpcServer::Stop()
{
	if (!thread_.joinable())
		return;
	stop_ = true;
	Wake();
	thread_.join();
	// Last words, e.g. the reply to "quit": deliver what is still in the outbox.
	{
		std::lock_guard<std::mutex> lock(outbox_mutex_);
		for (auto const &[target, line] : outbox_)
			for (auto &[id, client] : clients_)
				if (target == kBroadcast || target == id)
					client.out += line;
		outbox_.clear();
	}
	for (auto &[id, client] : clients_)
	{
		Flush(client);
		::close(client.fd);
	}
	clients_.clear();
	::close(listen_fd_);
	::close(wake_fd_);
	::unlink(path_.c_str());
}

void IpcServer::Send(int client, json const &message)
{
	{
		std::lock_guard<std::mutex> lock(outbox_mutex_);
		outbox_.emplace_back(client, to_line(message));
	}
	Wake();
}

void IpcServer::Wake()
{
	uint64_t one = 1;
	[[maybe_unused]] ssize_t n = ::write(wake_fd_, &one, sizeof(one));
}

void IpcServer::Loop()
{
	while (!stop_)
	{
		std::vector<pollfd> fds = { { listen_fd_, POLLIN, 0 }, { wake_fd_, POLLIN, 0 } };
		std::vector<int> ids;
		for (auto const &[id, client] : clients_)
		{
			short events = POLLIN | (client.out.empty() ? 0 : POLLOUT);
			fds.push_back({ client.fd, events, 0 });
			ids.push_back(id);
		}

		if (::poll(fds.data(), fds.size(), -1) < 0)
		{
			if (errno == EINTR)
				continue;
			std::cerr << errno_string("pycam-engine: ipc poll") << "\n";
			return;
		}

		if (fds[1].revents & POLLIN)
		{
			uint64_t count;
			[[maybe_unused]] ssize_t n = ::read(wake_fd_, &count, sizeof(count));
		}
		{
			std::lock_guard<std::mutex> lock(outbox_mutex_);
			for (auto const &[target, line] : outbox_)
			{
				for (auto &[id, client] : clients_)
					if (target == kBroadcast || target == id)
						client.out += line;
			}
			outbox_.clear();
		}
		if (fds[0].revents & POLLIN)
			Accept();

		for (size_t i = 0; i < ids.size(); i++)
		{
			auto it = clients_.find(ids[i]);
			if (it == clients_.end())
				continue;
			bool alive = true;
			if (fds[i + 2].revents & (POLLIN | POLLHUP | POLLERR))
				alive = Read(ids[i], it->second);
			if (!alive)
			{
				::close(it->second.fd);
				clients_.erase(it);
			}
		}
		for (auto it = clients_.begin(); it != clients_.end();)
		{
			if (Flush(it->second))
				++it;
			else
			{
				::close(it->second.fd);
				it = clients_.erase(it);
			}
		}
	}
}

void IpcServer::Accept()
{
	for (;;)
	{
		int fd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
		if (fd < 0)
			return; // EAGAIN: no more pending connections
		clients_[next_client_++] = Client { fd, {}, {} };
	}
}

bool IpcServer::Read(int id, Client &client)
{
	char buf[4096];
	for (;;)
	{
		ssize_t n = ::recv(client.fd, buf, sizeof(buf), 0);
		if (n == 0)
			return false;
		if (n < 0)
		{
			if (errno == EAGAIN || errno == EWOULDBLOCK)
				break;
			if (errno == EINTR)
				continue;
			return false;
		}
		client.in.append(buf, n);
		if (client.in.size() > kMaxInput)
			return false;
	}

	size_t start = 0, end;
	while ((end = client.in.find('\n', start)) != std::string::npos)
	{
		std::string line = client.in.substr(start, end - start);
		start = end + 1;
		if (line.find_first_not_of(" \t\r") == std::string::npos)
			continue;
		json message = json::parse(line, nullptr, false);
		if (message.is_discarded() || !message.is_object())
			client.out += to_line({ { "ok", false }, { "error", "bad_request" }, { "detail", "invalid JSON object" } });
		else
			handler_(id, std::move(message));
	}
	client.in.erase(0, start);
	return true;
}

bool IpcServer::Flush(Client &client)
{
	while (!client.out.empty())
	{
		ssize_t n = ::send(client.fd, client.out.data(), client.out.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
		if (n < 0)
		{
			if (errno == EAGAIN || errno == EWOULDBLOCK)
				break;
			if (errno == EINTR)
				continue;
			return false;
		}
		client.out.erase(0, n);
	}
	return client.out.size() <= kMaxOutput;
}

} // namespace pycam
